"""FastAPI webhook receiver. **Owned by Agent F.**

Endpoints:

``POST /webhook``
    GitHub ``pull_request`` events. Verifies ``X-Hub-Signature-256`` against the
    **raw** body before parsing anything, handles the ``opened``,
    ``synchronize`` and ``reopened`` actions, and hands the work to
    :mod:`mergesignal.service.worker` in the background. Returns 202 immediately
    — GitHub times webhook deliveries out after 10 seconds, and analysis can
    take longer.
``GET /healthz``
    Liveness probe: reports version and whether GitHub credentials are present.

Everything is configured from the environment (NFR-5): ``MERGESIGNAL_APP_ID``,
``MERGESIGNAL_PRIVATE_KEY``, ``MERGESIGNAL_WEBHOOK_SECRET``.

Security posture: an unsigned or mis-signed request gets a 401 and is *not*
processed; a missing webhook secret makes the service refuse to start rather
than silently accepting anonymous traffic.
"""

from __future__ import annotations

from typing import Any

#: ``pull_request`` actions that trigger an analysis run.
HANDLED_ACTIONS: frozenset[str] = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


class ServiceSettings:
    """Environment-derived service configuration.

    Reads the variables named by :class:`~mergesignal.config.GitHubConfig` and
    validates that the webhook secret is present.
    """

    def __init__(self, *, app_id: str | None = None, private_key: str | None = None, webhook_secret: str | None = None, token: str | None = None, api_url: str = "https://api.github.com", work_dir: str | None = None, run_timeout_seconds: float = 300.0) -> None:
        raise NotImplementedError

    @classmethod
    def from_env(cls) -> ServiceSettings:
        """Build from ``os.environ``.

        :raises RuntimeError: no webhook secret configured.
        """
        raise NotImplementedError

    @property
    def has_credentials(self) -> bool:
        """``True`` when either App auth or a PAT is available."""
        raise NotImplementedError


def create_app(settings: ServiceSettings | None = None) -> Any:
    """Build and return the FastAPI application.

    A factory rather than a module-level singleton so tests can inject settings
    and a mocked GitHub transport via ``TestClient(create_app(settings))``.
    """
    raise NotImplementedError


def handle_pull_request_event(payload: dict[str, Any], settings: ServiceSettings) -> dict[str, Any]:
    """Process one verified ``pull_request`` webhook payload.

    Ignores unhandled actions and draft PRs (configurable) by returning a
    ``{"status": "ignored", "reason": ...}`` dict rather than raising, so GitHub
    sees a 2xx and does not retry a delivery we deliberately skipped.
    """
    raise NotImplementedError


def run(host: str = "127.0.0.1", port: int = 8000, *, reload: bool = False, log_level: str = "info") -> None:
    """Run the uvicorn server. Called by ``mergesignal serve``."""
    raise NotImplementedError
