"""GitHub App authentication and webhook verification. **Owned by Agent F.**

Two independent jobs:

*Auth*: sign a short-lived JWT with the App's private key, exchange it for an
installation access token, cache the token until shortly before it expires.

*Webhook verification*: constant-time HMAC-SHA256 comparison of the
``X-Hub-Signature-256`` header against the raw request body. This must use the
**raw bytes**, not a re-serialised JSON dict, or the signature will never match.

Secrets are read from the environment only (NFR-5). ``MERGESIGNAL_PRIVATE_KEY``
may hold either the PEM text itself or a path to a PEM file — both are common in
deployment setups, so :func:`load_private_key` accepts either.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mergesignal.github.client import GitHubError

#: GitHub caps App JWT lifetime at 10 minutes; stay under it with clock skew room.
JWT_LIFETIME_SECONDS = 540

#: Renew an installation token this many seconds before it actually expires.
TOKEN_REFRESH_MARGIN_SECONDS = 300

#: Header carrying the HMAC-SHA256 webhook signature.
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: ``iat`` is back-dated by this many seconds so a slightly fast clock on our
#: side does not make GitHub reject the JWT as issued in the future.
JWT_CLOCK_SKEW_SECONDS = 60

#: Marker every PEM body contains; used to tell inline PEM from a file path.
_PEM_MARKER = "-----BEGIN"


@dataclass(frozen=True)
class InstallationToken:
    """A cached installation access token."""

    token: str
    expires_at: float
    """POSIX timestamp of expiry, as reported by GitHub."""

    installation_id: int = 0

    @property
    def is_expired(self) -> bool:
        """``True`` once the token is within the refresh margin of expiry."""
        return time.time() >= self.expires_at - TOKEN_REFRESH_MARGIN_SECONDS


class GitHubAppAuth:
    """Mints and caches installation tokens for a GitHub App.

    :param app_id: numeric App id.
    :param private_key: PEM contents or a path to a PEM file.
    :param api_url: REST base, overridable for GitHub Enterprise.
    :param transport: optional ``httpx`` transport, injected by tests.

    Thread-safe: the token cache is guarded by a lock because the webhook
    service runs analyses on background threads that all need a token.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        api_url: str = "https://api.github.com",
        transport: Any = None,
        timeout: float = 20.0,
    ) -> None:
        app_id = str(app_id).strip()
        if not app_id:
            raise ValueError("app_id must not be empty")
        self.app_id = app_id
        self.private_key = load_private_key(private_key)
        self.api_url = api_url.rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport
        self._lock = threading.Lock()
        self._tokens: dict[int, InstallationToken] = {}
        self._installations: dict[str, int] = {}

    def __repr__(self) -> str:  # pragma: no cover - trivial, and must not leak the key
        return f"GitHubAppAuth(app_id={self.app_id!r})"

    @classmethod
    def from_env(
        cls,
        *,
        app_id_env: str = "MERGESIGNAL_APP_ID",
        private_key_env: str = "MERGESIGNAL_PRIVATE_KEY",
        api_url: str = "https://api.github.com",
        transport: Any = None,
    ) -> GitHubAppAuth | None:
        """Build from environment variables, or ``None`` when unconfigured.

        Returning ``None`` rather than raising keeps the tool offline-capable:
        no App configured simply means no App auth. A *partially* configured App
        (id without key, or a key that cannot be read) is a different matter —
        that is a deployment mistake and raises.
        """
        app_id = (os.environ.get(app_id_env) or "").strip()
        private_key = os.environ.get(private_key_env) or ""
        if not app_id and not private_key.strip():
            return None
        if not app_id:
            raise ValueError(f"{private_key_env} is set but {app_id_env} is not")
        if not private_key.strip():
            raise ValueError(f"{app_id_env} is set but {private_key_env} is not")
        return cls(app_id, private_key, api_url=api_url, transport=transport)

    # ------------------------------------------------------------------ jwt

    def create_jwt(self, *, now: float | None = None) -> str:
        """Sign an RS256 App JWT with ``iat`` back-dated 60s for clock skew.

        :raises ValueError: the private key is unusable.
        """
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise ValueError("PyJWT with cryptography is required for GitHub App auth") from exc

        issued_at = int(now if now is not None else time.time()) - JWT_CLOCK_SKEW_SECONDS
        payload = {
            "iat": issued_at,
            "exp": issued_at + JWT_LIFETIME_SECONDS,
            "iss": self.app_id,
        }
        try:
            token = jwt.encode(payload, self.private_key, algorithm="RS256")
        except Exception as exc:
            raise ValueError(f"cannot sign App JWT: {type(exc).__name__}: {exc}") from exc
        return token if isinstance(token, str) else token.decode("ascii")

    # --------------------------------------------------------------- tokens

    def installation_token(self, installation_id: int, *, force_refresh: bool = False) -> str:
        """Return a valid installation token, minting one if the cache is cold.

        :raises GitHubError: GitHub rejected the JWT or the installation id.
        """
        installation_id = int(installation_id)
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and not cached.is_expired and not force_refresh:
                return cached.token

        minted = self._mint_token(installation_id)
        with self._lock:
            self._tokens[installation_id] = minted
        return minted.token

    def token_for_repo(self, repo_slug: str) -> str:
        """Resolve ``owner/name`` to its installation and return a token."""
        return self.installation_token(self.installation_id_for_repo(repo_slug))

    def installation_id_for_repo(self, repo_slug: str) -> int:
        """Look up (and cache) the installation id covering ``owner/name``.

        :raises GitHubError: the App is not installed on that repository.
        """
        with self._lock:
            cached = self._installations.get(repo_slug)
        if cached is not None:
            return cached

        data = self._api("GET", f"/repos/{repo_slug}/installation")
        installation_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(installation_id, int):
            raise GitHubError(f"no installation found for {repo_slug!r}", body=data)
        with self._lock:
            self._installations[repo_slug] = installation_id
        return installation_id

    def _mint_token(self, installation_id: int) -> InstallationToken:
        """POST to the access-tokens endpoint and parse the response."""
        data = self._api("POST", f"/app/installations/{installation_id}/access_tokens")
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubError(f"installation {installation_id} returned no token", body=data)
        expires_at = _parse_expiry(data.get("expires_at"))
        return InstallationToken(token=token, expires_at=expires_at, installation_id=installation_id)

    def _api(self, method: str, path: str) -> Any:
        """Call the REST API authenticated with a freshly signed App JWT."""
        import httpx

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.create_jwt()}",
        }
        try:
            with httpx.Client(base_url=self.api_url, timeout=self.timeout, transport=self._transport) as client:
                response = client.request(method, path, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub App request failed: {exc}") from exc

        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text

        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else str(body)[:200]
            raise GitHubError(
                f"GitHub App auth failed ({response.status_code}) for {path}: {message}",
                status_code=response.status_code,
                body=body,
            )
        return body


def _parse_expiry(value: Any) -> float:
    """Convert GitHub's ISO-8601 ``expires_at`` into a POSIX timestamp.

    An unparseable or absent value falls back to "one hour from now", which is
    GitHub's documented installation-token lifetime; combined with
    :data:`TOKEN_REFRESH_MARGIN_SECONDS` that errs towards minting again too
    early rather than using a dead token.
    """
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
    return time.time() + 3600.0


def load_private_key(value: str) -> str:
    """Return PEM text from either inline PEM or a filesystem path.

    Literal ``\\n`` escapes (how PEMs survive most CI secret stores) are
    converted to real newlines.

    :raises ValueError: neither a readable file nor PEM-looking text.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("private key is empty")

    unescaped = value.replace("\\n", "\n")
    if _PEM_MARKER in unescaped:
        return unescaped.strip() + "\n"

    candidate = Path(value.strip()).expanduser()
    try:
        is_file = candidate.is_file()
    except OSError:  # path too long, embedded NUL, ...
        is_file = False
    if is_file:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read private key file {candidate}: {exc}") from exc
        if _PEM_MARKER not in text:
            raise ValueError(f"{candidate} does not look like a PEM private key")
        return text.strip() + "\n"

    raise ValueError("private key is neither a readable file path nor PEM text")


def verify_webhook(payload: bytes, signature: str | None, secret: str) -> bool:
    """Constant-time verification of a GitHub webhook signature.

    :param payload: the **raw** request body bytes.
    :param signature: the ``X-Hub-Signature-256`` header value, including the
        ``sha256=`` prefix. ``None`` or malformed -> ``False``.
    :param secret: shared webhook secret.
    :returns: ``True`` only for a valid signature. Uses
        :func:`hmac.compare_digest`; never short-circuits on length, never
        raises on garbage input, and an empty ``secret`` always returns ``False``
        rather than accidentally accepting unsigned traffic.
    """
    if not secret or not isinstance(secret, str):
        return False
    if not signature or not isinstance(signature, str):
        return False

    scheme, separator, digest = signature.strip().partition("=")
    if not separator or scheme.strip().lower() != "sha256" or not digest:
        return False

    try:
        expected = _digest(payload, secret)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, digest.strip().lower())


def sign_webhook(payload: bytes, secret: str) -> str:
    """Produce a ``sha256=...`` signature for ``payload`` — used by tests."""
    return f"sha256={_digest(payload, secret)}"


def _digest(payload: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of ``payload`` under ``secret``."""
    body = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
