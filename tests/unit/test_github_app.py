"""Unit tests for GitHub App auth and webhook signature verification.

The signature tests are the security-critical ones (FR-8): valid, invalid,
tampered payload, tampered signature, wrong secret, missing header. Everything
here is offline — the token-exchange tests drive an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mergesignal.github.app import (
    JWT_LIFETIME_SECONDS,
    SIGNATURE_HEADER,
    TOKEN_REFRESH_MARGIN_SECONDS,
    GitHubAppAuth,
    InstallationToken,
    load_private_key,
    sign_webhook,
    verify_webhook,
)
from mergesignal.github.client import GitHubError

SECRET = "it's a secret to everybody"
PAYLOAD = b'{"action":"opened","number":7}'


# ------------------------------------------------------------------- keys


@pytest.fixture(scope="module")
def rsa_key_pair() -> tuple[str, str]:
    """A throwaway RSA key pair as ``(private PEM, public PEM)``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


@pytest.fixture
def private_pem(rsa_key_pair: tuple[str, str]) -> str:
    return rsa_key_pair[0]


@pytest.fixture
def public_pem(rsa_key_pair: tuple[str, str]) -> str:
    return rsa_key_pair[1]


# -------------------------------------------------------- webhook signatures


def test_verify_webhook_accepts_a_valid_signature() -> None:
    assert verify_webhook(PAYLOAD, sign_webhook(PAYLOAD, SECRET), SECRET) is True


def test_sign_webhook_uses_the_sha256_prefix() -> None:
    signature = sign_webhook(PAYLOAD, SECRET)
    scheme, _, digest = signature.partition("=")
    assert scheme == "sha256"
    assert len(digest) == 64
    assert SIGNATURE_HEADER == "X-Hub-Signature-256"


def test_verify_webhook_rejects_a_tampered_payload() -> None:
    signature = sign_webhook(PAYLOAD, SECRET)
    tampered = PAYLOAD.replace(b"opened", b"closed")
    assert verify_webhook(tampered, signature, SECRET) is False


def test_verify_webhook_rejects_a_tampered_signature() -> None:
    signature = sign_webhook(PAYLOAD, SECRET)
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    assert verify_webhook(PAYLOAD, flipped, SECRET) is False


def test_verify_webhook_rejects_the_wrong_secret() -> None:
    assert verify_webhook(PAYLOAD, sign_webhook(PAYLOAD, "other"), SECRET) is False


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "   ",
        "deadbeef",  # no scheme
        "sha1=" + sign_webhook(PAYLOAD, SECRET).partition("=")[2],  # wrong algorithm
        "sha256=",  # empty digest
        "sha256",  # no separator
        "=" + sign_webhook(PAYLOAD, SECRET).partition("=")[2],  # empty scheme
        "sha256=zzzz",  # not hex
    ],
)
def test_verify_webhook_rejects_malformed_headers(signature: str | None) -> None:
    assert verify_webhook(PAYLOAD, signature, SECRET) is False


def test_verify_webhook_rejects_an_empty_secret() -> None:
    """An unset secret must never accept traffic, not even correctly signed."""
    assert verify_webhook(PAYLOAD, sign_webhook(PAYLOAD, ""), "") is False


def test_verify_webhook_accepts_uppercase_hex() -> None:
    signature = sign_webhook(PAYLOAD, SECRET).upper().replace("SHA256", "sha256")
    assert verify_webhook(PAYLOAD, signature, SECRET) is True


def test_verify_webhook_is_raw_byte_sensitive() -> None:
    """Re-serialised JSON has different bytes and must not verify."""
    signature = sign_webhook(PAYLOAD, SECRET)
    reserialised = json.dumps(json.loads(PAYLOAD)).encode()
    assert reserialised != PAYLOAD
    assert verify_webhook(reserialised, signature, SECRET) is False


def test_verify_webhook_handles_empty_body() -> None:
    assert verify_webhook(b"", sign_webhook(b"", SECRET), SECRET) is True


# ------------------------------------------------------------ private keys


def test_load_private_key_accepts_inline_pem(private_pem: str) -> None:
    assert load_private_key(private_pem).startswith("-----BEGIN")


def test_load_private_key_unescapes_newlines(private_pem: str) -> None:
    escaped = private_pem.replace("\n", "\\n")
    assert load_private_key(escaped) == private_pem.strip() + "\n"


def test_load_private_key_reads_a_file(tmp_path: Any, private_pem: str) -> None:
    pem_file = tmp_path / "app.pem"
    pem_file.write_text(private_pem, encoding="utf-8")
    assert load_private_key(str(pem_file)) == private_pem.strip() + "\n"


def test_load_private_key_rejects_a_non_pem_file(tmp_path: Any) -> None:
    junk = tmp_path / "not.pem"
    junk.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="does not look like a PEM"):
        load_private_key(str(junk))


@pytest.mark.parametrize("value", ["", "   ", "/nonexistent/key.pem"])
def test_load_private_key_rejects_garbage(value: str) -> None:
    with pytest.raises(ValueError):
        load_private_key(value)


# -------------------------------------------------------------------- jwt


def test_create_jwt_is_verifiable_and_well_formed(private_pem: str, public_pem: str) -> None:
    auth = GitHubAppAuth("12345", private_pem)
    now = time.time()
    token = auth.create_jwt(now=now)

    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_aud": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(now) - 60, "iat must be back-dated for clock skew"
    assert claims["exp"] - claims["iat"] == JWT_LIFETIME_SECONDS
    assert claims["exp"] - claims["iat"] < 600, "GitHub caps App JWTs at 10 minutes"


def test_create_jwt_rejects_a_broken_key() -> None:
    auth = GitHubAppAuth(
        "1", "-----BEGIN RSA PRIVATE KEY-----\nnope\n-----END RSA PRIVATE KEY-----"
    )
    with pytest.raises(ValueError, match="cannot sign App JWT"):
        auth.create_jwt()


def test_app_auth_rejects_an_empty_app_id(private_pem: str) -> None:
    with pytest.raises(ValueError, match="app_id"):
        GitHubAppAuth("  ", private_pem)


def test_repr_does_not_leak_the_private_key(private_pem: str) -> None:
    auth = GitHubAppAuth("42", private_pem)
    assert "PRIVATE" not in repr(auth)


# ------------------------------------------------------------ from_env


def test_from_env_returns_none_when_unconfigured() -> None:
    assert GitHubAppAuth.from_env() is None


def test_from_env_builds_from_environment(
    monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    monkeypatch.setenv("MERGESIGNAL_APP_ID", "999")
    monkeypatch.setenv("MERGESIGNAL_PRIVATE_KEY", private_pem)
    auth = GitHubAppAuth.from_env()
    assert auth is not None
    assert auth.app_id == "999"


def test_from_env_rejects_a_half_configured_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERGESIGNAL_APP_ID", "999")
    with pytest.raises(ValueError, match="MERGESIGNAL_PRIVATE_KEY"):
        GitHubAppAuth.from_env()


# ----------------------------------------------------------- token caching


def test_installation_token_expiry_margin() -> None:
    fresh = InstallationToken(token="t", expires_at=time.time() + TOKEN_REFRESH_MARGIN_SECONDS + 60)
    stale = InstallationToken(token="t", expires_at=time.time() + TOKEN_REFRESH_MARGIN_SECONDS - 1)
    assert fresh.is_expired is False
    assert stale.is_expired is True, "a token inside the refresh margin counts as expired"


def _token_transport(
    calls: list[httpx.Request], *, expires_in: float = 3600.0
) -> httpx.MockTransport:
    """Mock ``/app/installations/.../access_tokens`` and ``/repos/.../installation``."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 4242})
        if request.url.path.endswith("/access_tokens"):
            expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))
            return httpx.Response(201, json={"token": f"ghs_{len(calls)}", "expires_at": expiry})
        return httpx.Response(404, json={"message": "nope"})

    return httpx.MockTransport(handler)


def test_installation_token_is_cached(private_pem: str) -> None:
    calls: list[httpx.Request] = []
    auth = GitHubAppAuth("1", private_pem, transport=_token_transport(calls))

    first = auth.installation_token(4242)
    second = auth.installation_token(4242)

    assert first == second
    assert len(calls) == 1, "the cached token must not be re-minted"


def test_installation_token_force_refresh_mints_again(private_pem: str) -> None:
    calls: list[httpx.Request] = []
    auth = GitHubAppAuth("1", private_pem, transport=_token_transport(calls))

    first = auth.installation_token(4242)
    second = auth.installation_token(4242, force_refresh=True)

    assert first != second
    assert len(calls) == 2


def test_installation_token_refreshes_inside_the_margin(private_pem: str) -> None:
    """A token that expires sooner than the refresh margin is never reused."""
    calls: list[httpx.Request] = []
    auth = GitHubAppAuth("1", private_pem, transport=_token_transport(calls, expires_in=60))

    auth.installation_token(4242)
    auth.installation_token(4242)

    assert len(calls) == 2


def test_installation_token_sends_a_bearer_jwt(private_pem: str, public_pem: str) -> None:
    calls: list[httpx.Request] = []
    auth = GitHubAppAuth("77", private_pem, transport=_token_transport(calls))

    auth.installation_token(4242)

    header = calls[0].headers["Authorization"]
    assert header.startswith("Bearer ")
    claims = jwt.decode(header.removeprefix("Bearer "), public_pem, algorithms=["RS256"])
    assert claims["iss"] == "77"


def test_token_for_repo_resolves_and_caches_the_installation(private_pem: str) -> None:
    calls: list[httpx.Request] = []
    auth = GitHubAppAuth("1", private_pem, transport=_token_transport(calls))

    auth.token_for_repo("acme/widgets")
    auth.token_for_repo("acme/widgets")

    paths = [c.url.path for c in calls]
    assert paths.count("/repos/acme/widgets/installation") == 1
    assert paths.count("/app/installations/4242/access_tokens") == 1


def test_token_exchange_failure_raises_github_error(private_pem: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, json={"message": "Not Found"})
    )
    auth = GitHubAppAuth("1", private_pem, transport=transport)
    with pytest.raises(GitHubError) as exc_info:
        auth.installation_token(1)
    assert exc_info.value.status_code == 404


def test_missing_token_in_response_raises(private_pem: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(201, json={"expires_at": "2030-01-01T00:00:00Z"})
    )
    auth = GitHubAppAuth("1", private_pem, transport=transport)
    with pytest.raises(GitHubError, match="no token"):
        auth.installation_token(1)
