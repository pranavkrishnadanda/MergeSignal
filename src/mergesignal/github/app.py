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

from dataclasses import dataclass

#: GitHub caps App JWT lifetime at 10 minutes; stay under it with clock skew room.
JWT_LIFETIME_SECONDS = 540

#: Renew an installation token this many seconds before it actually expires.
TOKEN_REFRESH_MARGIN_SECONDS = 300

#: Header carrying the HMAC-SHA256 webhook signature.
SIGNATURE_HEADER = "X-Hub-Signature-256"


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
        raise NotImplementedError


class GitHubAppAuth:
    """Mints and caches installation tokens for a GitHub App.

    :param app_id: numeric App id.
    :param private_key: PEM contents or a path to a PEM file.
    :param api_url: REST base, overridable for GitHub Enterprise.
    """

    def __init__(self, app_id: str, private_key: str, *, api_url: str = "https://api.github.com") -> None:
        raise NotImplementedError

    @classmethod
    def from_env(cls, *, app_id_env: str = "MERGESIGNAL_APP_ID", private_key_env: str = "MERGESIGNAL_PRIVATE_KEY", api_url: str = "https://api.github.com") -> GitHubAppAuth | None:
        """Build from environment variables, or ``None`` when unconfigured.

        Returning ``None`` rather than raising keeps the tool offline-capable:
        no App configured simply means no App auth.
        """
        raise NotImplementedError

    def create_jwt(self, *, now: float | None = None) -> str:
        """Sign an RS256 App JWT with ``iat`` back-dated 60s for clock skew.

        :raises ValueError: the private key is unusable.
        """
        raise NotImplementedError

    def installation_token(self, installation_id: int, *, force_refresh: bool = False) -> str:
        """Return a valid installation token, minting one if the cache is cold.

        :raises GitHubError: GitHub rejected the JWT or the installation id.
        """
        raise NotImplementedError

    def token_for_repo(self, repo_slug: str) -> str:
        """Resolve ``owner/name`` to its installation and return a token."""
        raise NotImplementedError


def load_private_key(value: str) -> str:
    """Return PEM text from either inline PEM or a filesystem path.

    Literal ``\\n`` escapes (how PEMs survive most CI secret stores) are
    converted to real newlines.

    :raises ValueError: neither a readable file nor PEM-looking text.
    """
    raise NotImplementedError


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
    raise NotImplementedError


def sign_webhook(payload: bytes, secret: str) -> str:
    """Produce a ``sha256=...`` signature for ``payload`` — used by tests."""
    raise NotImplementedError
