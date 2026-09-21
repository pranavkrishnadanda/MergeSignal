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

import json
import logging
import os
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from mergesignal import __version__
from mergesignal.github.app import SIGNATURE_HEADER, verify_webhook

# FastAPI is imported at module scope on purpose: it resolves route annotations
# against this module's globals, so ``Request``/``BackgroundTasks`` imported
# inside create_app() would be mistaken for query parameters. Importing this
# module is still lazy — the CLI only does it inside ``mergesignal serve``.

#: ``pull_request`` actions that trigger an analysis run.
HANDLED_ACTIONS: frozenset[str] = frozenset(
    {"opened", "synchronize", "reopened", "ready_for_review"}
)

#: Header naming the webhook event type.
EVENT_HEADER = "X-GitHub-Event"

#: Header carrying GitHub's delivery id; echoed into logs for traceability.
DELIVERY_HEADER = "X-GitHub-Delivery"

logger = logging.getLogger("mergesignal.service")


class ServiceSettings:
    """Environment-derived service configuration.

    Reads the variables named by :class:`~mergesignal.config.GitHubConfig` and
    validates that the webhook secret is present.

    :param transport: ``httpx`` transport handed to every
        :class:`~mergesignal.github.client.GitHubClient` the service builds —
        the seam tests use to mock the GitHub API.
    :param clone_url: override the URL the worker fetches PR refs from, for
        mirrors, Enterprise setups whose git host differs from the API host, and
        tests cloning from a local path.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        private_key: str | None = None,
        webhook_secret: str | None = None,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        work_dir: str | None = None,
        run_timeout_seconds: float = 300.0,
        ignore_drafts: bool = True,
        post_comment: bool = True,
        transport: Any = None,
        clone_url: str | None = None,
    ) -> None:
        if not webhook_secret or not webhook_secret.strip():
            raise RuntimeError(
                "no webhook secret configured: set MERGESIGNAL_WEBHOOK_SECRET. "
                "Refusing to start rather than accept unsigned webhook traffic."
            )
        self.app_id = (app_id or "").strip() or None
        self.private_key = private_key or None
        self.webhook_secret = webhook_secret
        self.token = token or None
        self.api_url = api_url.rstrip("/")
        self.work_dir = work_dir or None
        self.run_timeout_seconds = float(run_timeout_seconds)
        self.ignore_drafts = bool(ignore_drafts)
        self.post_comment = bool(post_comment)
        self.transport = transport
        self.clone_url = clone_url

    def __repr__(self) -> str:  # pragma: no cover - trivial; never print secrets
        return f"ServiceSettings(api_url={self.api_url!r}, credentials={self.has_credentials})"

    @classmethod
    def from_env(cls) -> ServiceSettings:
        """Build from ``os.environ``.

        :raises RuntimeError: no webhook secret configured.
        """
        timeout = os.environ.get("MERGESIGNAL_RUN_TIMEOUT")
        try:
            run_timeout = float(timeout) if timeout else 300.0
        except ValueError as exc:
            raise RuntimeError(
                f"MERGESIGNAL_RUN_TIMEOUT must be a number, got {timeout!r}"
            ) from exc

        return cls(
            app_id=os.environ.get("MERGESIGNAL_APP_ID"),
            private_key=os.environ.get("MERGESIGNAL_PRIVATE_KEY"),
            webhook_secret=os.environ.get("MERGESIGNAL_WEBHOOK_SECRET"),
            token=os.environ.get("GITHUB_TOKEN"),
            api_url=os.environ.get("MERGESIGNAL_API_URL", "https://api.github.com"),
            work_dir=os.environ.get("MERGESIGNAL_WORK_DIR"),
            run_timeout_seconds=run_timeout,
        )

    @property
    def has_credentials(self) -> bool:
        """``True`` when either App auth or a PAT is available."""
        return bool(self.token) or bool(self.app_id and self.private_key)

    def token_for(self, repo_slug: str) -> str | None:
        """Resolve a token for ``repo_slug``: PAT, else an installation token.

        Returns ``None`` when nothing is configured; the client then makes
        unauthenticated (public, heavily rate-limited) requests.
        """
        if self.token:
            return self.token
        if not (self.app_id and self.private_key):
            return None
        from mergesignal.github.app import GitHubAppAuth

        auth = GitHubAppAuth(
            self.app_id, self.private_key, api_url=self.api_url, transport=self.transport
        )
        return auth.token_for_repo(repo_slug)


def create_app(settings: ServiceSettings | None = None) -> Any:
    """Build and return the FastAPI application.

    A factory rather than a module-level singleton so tests can inject settings
    and a mocked GitHub transport via ``TestClient(create_app(settings))``.
    """
    config = settings or ServiceSettings.from_env()

    app = FastAPI(
        title="MergeSignal",
        version=__version__,
        description="Pre-merge intelligence webhook service.",
    )
    app.state.settings = config

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness probe — never touches the network."""
        return {
            "status": "ok",
            "version": __version__,
            "credentials": config.has_credentials,
            "app_auth": bool(config.app_id and config.private_key),
        }

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
        """Verify, classify and enqueue one GitHub webhook delivery."""
        raw = await request.body()
        signature = request.headers.get(SIGNATURE_HEADER)
        if not verify_webhook(raw, signature, config.webhook_secret):
            logger.warning(
                "rejected webhook delivery %s: bad signature", request.headers.get(DELIVERY_HEADER)
            )
            return JSONResponse(
                {"status": "rejected", "reason": "invalid signature"}, status_code=401
            )

        event = request.headers.get(EVENT_HEADER, "")
        if event == "ping":
            return JSONResponse({"status": "pong", "version": __version__}, status_code=200)
        if event != "pull_request":
            return JSONResponse(
                {"status": "ignored", "reason": f"unhandled event {event!r}"}, status_code=200
            )

        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return JSONResponse(
                {"status": "rejected", "reason": "body is not valid JSON"}, status_code=400
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "rejected", "reason": "body is not a JSON object"}, status_code=400
            )

        skip = classify_event(payload, config)
        if skip is not None:
            return JSONResponse(skip, status_code=200)

        repo_slug, pr_number = _target(payload)
        background.add_task(handle_pull_request_event, payload, config)
        return JSONResponse(
            {
                "status": "accepted",
                "repo": repo_slug,
                "pr": pr_number,
                "action": payload.get("action"),
            },
            status_code=202,
        )

    return app


def classify_event(payload: dict[str, Any], settings: ServiceSettings) -> dict[str, Any] | None:
    """Decide whether a ``pull_request`` payload deserves an analysis run.

    :returns: ``None`` when the event should be processed, otherwise the
        ``{"status": "ignored", "reason": ...}`` body to return.
    """
    action = payload.get("action")
    if action not in HANDLED_ACTIONS:
        return {"status": "ignored", "reason": f"unhandled action {action!r}"}

    pull = payload.get("pull_request") or {}
    if not isinstance(pull, dict) or not pull.get("number"):
        return {"status": "ignored", "reason": "payload carries no pull request"}
    if settings.ignore_drafts and bool(pull.get("draft")) and action != "ready_for_review":
        return {"status": "ignored", "reason": "pull request is a draft"}

    repo_slug, pr_number = _target(payload)
    if not repo_slug or pr_number is None:
        return {"status": "ignored", "reason": "payload carries no repository"}
    return None


def handle_pull_request_event(payload: dict[str, Any], settings: ServiceSettings) -> dict[str, Any]:
    """Process one verified ``pull_request`` webhook payload.

    Ignores unhandled actions and draft PRs (configurable) by returning a
    ``{"status": "ignored", "reason": ...}`` dict rather than raising, so GitHub
    sees a 2xx and does not retry a delivery we deliberately skipped.
    """
    skip = classify_event(payload, settings)
    if skip is not None:
        return skip

    repo_slug, pr_number = _target(payload)
    if (
        repo_slug is None or pr_number is None
    ):  # pragma: no cover - classify_event already rejected this
        return {"status": "ignored", "reason": "payload carries no repository"}

    from mergesignal.service.worker import analyze_pull_request

    try:
        token = settings.token_for(repo_slug)
    except Exception as exc:
        logger.exception("could not obtain a token for %s", repo_slug)
        return {
            "status": "error",
            "repo": repo_slug,
            "pr": pr_number,
            "error": f"{type(exc).__name__}: {exc}",
        }

    result = analyze_pull_request(
        repo_slug,
        pr_number,
        token=token,
        api_url=settings.api_url,
        work_dir=settings.work_dir,
        timeout_seconds=settings.run_timeout_seconds,
        post_comment=settings.post_comment,
        transport=settings.transport,
        clone_url=settings.clone_url,
    )

    if not result.ok:
        logger.error("analysis failed for %s#%s: %s", repo_slug, pr_number, result.error)
        return {"status": "error", "repo": repo_slug, "pr": pr_number, "error": result.error}

    logger.info("analysed %s#%s in %.2fs", repo_slug, pr_number, result.duration_seconds)
    return {
        "status": "analyzed",
        "repo": repo_slug,
        "pr": pr_number,
        "comment_id": result.comment_id,
        "duration_seconds": result.duration_seconds,
    }


def _target(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    """Extract ``(owner/name, pr number)`` from a webhook payload."""
    repository = payload.get("repository") or {}
    slug = repository.get("full_name") if isinstance(repository, dict) else None
    pull = payload.get("pull_request") or {}
    number = pull.get("number") if isinstance(pull, dict) else None
    if number is None:
        number = payload.get("number")
    try:
        pr_number = int(number) if number is not None else None
    except (TypeError, ValueError):
        pr_number = None
    return (slug if isinstance(slug, str) and slug else None), pr_number


def run(
    host: str = "127.0.0.1", port: int = 8000, *, reload: bool = False, log_level: str = "info"
) -> None:
    """Run the uvicorn server. Called by ``mergesignal serve``.

    Settings are validated *before* uvicorn starts so a missing webhook secret
    fails fast with a clear message rather than on the first delivery.
    """
    import uvicorn

    ServiceSettings.from_env()  # fail fast on a missing webhook secret
    logging.basicConfig(
        level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(
        "mergesignal.service.server:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )
