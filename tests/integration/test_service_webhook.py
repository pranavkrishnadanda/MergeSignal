"""End-to-end tests of the FastAPI webhook service.

A recorded ``pull_request`` delivery is signed with the service's secret, posted
through ``TestClient``, and the resulting background analysis runs for real
against a repository built by :class:`~tests.helpers.repo_builder.RepoBuilder`,
with only the GitHub REST API replaced by an ``httpx.MockTransport``.

``TestClient`` executes background tasks before returning from ``post()``, so
assertions about the comment upsert are made straight after the call.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mergesignal.github.app import SIGNATURE_HEADER, sign_webhook
from mergesignal.report.github_comment import COMMENT_MARKER
from mergesignal.service.server import (
    HANDLED_ACTIONS,
    ServiceSettings,
    classify_event,
    create_app,
    handle_pull_request_event,
)
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration

SECRET = "webhook-shared-secret"
SLUG = "acme/widgets"

#: A recorded ``pull_request`` delivery, trimmed to the fields GitHub always
#: sends and MergeSignal actually reads. Kept verbatim in the test module so the
#: fixture cannot drift away from the code that consumes it.
WEBHOOK_PAYLOAD: dict[str, Any] = json.loads(
    """
{
  "action": "opened",
  "number": 7,
  "pull_request": {
    "url": "https://api.github.com/repos/acme/widgets/pulls/7",
    "id": 1234567890,
    "node_id": "PR_kwDOA",
    "html_url": "https://github.com/acme/widgets/pull/7",
    "number": 7,
    "state": "open",
    "locked": false,
    "title": "Rename old_name to new_name",
    "user": {"login": "octocat", "id": 583231, "type": "User"},
    "body": "Renames the helper.",
    "created_at": "2024-05-01T10:00:00Z",
    "updated_at": "2024-05-01T10:00:00Z",
    "draft": false,
    "head": {
      "label": "acme:feature",
      "ref": "feature",
      "sha": "0000000000000000000000000000000000000000",
      "user": {"login": "octocat"},
      "repo": {"id": 42, "name": "widgets", "full_name": "acme/widgets", "clone_url": "https://github.com/acme/widgets.git"}
    },
    "base": {
      "label": "acme:main",
      "ref": "main",
      "sha": "1111111111111111111111111111111111111111",
      "user": {"login": "acme"},
      "repo": {"id": 42, "name": "widgets", "full_name": "acme/widgets", "clone_url": "https://github.com/acme/widgets.git"}
    },
    "merged": false,
    "mergeable": null,
    "additions": 1,
    "deletions": 1,
    "changed_files": 1
  },
  "repository": {
    "id": 42,
    "name": "widgets",
    "full_name": "acme/widgets",
    "private": false,
    "owner": {"login": "acme", "type": "Organization"},
    "html_url": "https://github.com/acme/widgets",
    "default_branch": "main"
  },
  "sender": {"login": "octocat", "type": "User"},
  "installation": {"id": 4242, "node_id": "MDIzOkl"}
}
"""
)


class FakeGitHub:
    """Mock GitHub REST API recording every call the service makes."""

    def __init__(self, existing_comments: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.existing = existing_comments or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.content:
            self.bodies.append(json.loads(request.read()))
        if path == f"/repos/{SLUG}/pulls/7":
            return httpx.Response(200, json=WEBHOOK_PAYLOAD["pull_request"])
        if path == f"/repos/{SLUG}/pulls":
            # The candidate itself is the only open PR — the worker must skip it
            # as its own peer and still emit a report.
            return httpx.Response(200, json=[WEBHOOK_PAYLOAD["pull_request"]])
        if path == f"/repos/{SLUG}/issues/7/comments" and request.method == "GET":
            return httpx.Response(200, json=self.existing)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 900})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 42})
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture
def upstream(builder: RepoBuilder) -> Path:
    """The repository the PR is against: ``main`` vs ``feature``."""
    return builder.scenario_rename_vs_new_caller().build()


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def settings(github: FakeGitHub, upstream: Path, tmp_path: Path) -> ServiceSettings:
    """Service settings pointed at the mocked API and the local repository."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return ServiceSettings(
        webhook_secret=SECRET,
        token="ghp_test",
        transport=httpx.MockTransport(github),
        clone_url=str(upstream),
        work_dir=str(work_dir),
    )


@pytest.fixture
def client(settings: ServiceSettings) -> TestClient:
    return TestClient(create_app(settings))


def post_webhook(
    client: TestClient,
    payload: dict[str, Any],
    *,
    secret: str = SECRET,
    event: str = "pull_request",
    signature: str | None = None,
    body: bytes | None = None,
) -> httpx.Response:
    """Sign and POST a webhook delivery exactly as GitHub would."""
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "00000000-0000-0000-0000-000000000000",
    }
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    else:
        headers[SIGNATURE_HEADER] = sign_webhook(raw, secret)
    return client.post("/webhook", content=raw, headers=headers)


# ------------------------------------------------------------------ health


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["credentials"] is True
    assert body["version"]


def test_healthz_reports_missing_credentials() -> None:
    with TestClient(create_app(ServiceSettings(webhook_secret=SECRET))) as client:
        body = client.get("/healthz").json()
    assert body["credentials"] is False
    assert body["app_auth"] is False


# -------------------------------------------------------------- signatures


def test_valid_signature_is_accepted_and_analysed(client: TestClient, github: FakeGitHub) -> None:
    response = post_webhook(client, WEBHOOK_PAYLOAD)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "repo": SLUG, "pr": 7, "action": "opened"}

    assert github.calls == [
        ("GET", f"/repos/{SLUG}/pulls/7"),
        ("GET", f"/repos/{SLUG}/pulls"),
        ("GET", f"/repos/{SLUG}/issues/7/comments"),
        ("POST", f"/repos/{SLUG}/issues/7/comments"),
    ]
    body = github.bodies[-1]["body"]
    assert body.startswith("## MergeSignal")
    assert COMMENT_MARKER in body


def test_existing_comment_is_updated_not_duplicated(
    settings: ServiceSettings, github: FakeGitHub
) -> None:
    github.existing = [
        {
            "id": 42,
            "body": f"previous report\n{COMMENT_MARKER}",
            "user": {"login": "bot"},
            "created_at": "2024-01-01T00:00:00Z",
        }
    ]
    payload = copy.deepcopy(WEBHOOK_PAYLOAD)
    payload["action"] = "synchronize"

    with TestClient(create_app(settings)) as client:
        response = post_webhook(client, payload)

    assert response.status_code == 202
    assert github.calls[-1] == ("PATCH", f"/repos/{SLUG}/issues/comments/42")
    assert "POST" not in github.methods, "a second comment must never be posted"
    assert COMMENT_MARKER in github.bodies[-1]["body"]


def test_invalid_signature_is_rejected(client: TestClient, github: FakeGitHub) -> None:
    response = post_webhook(client, WEBHOOK_PAYLOAD, secret="wrong-secret")

    assert response.status_code == 401
    assert response.json()["reason"] == "invalid signature"
    assert github.calls == [], "an unverified delivery must never reach GitHub"


def test_tampered_body_is_rejected(client: TestClient, github: FakeGitHub) -> None:
    raw = json.dumps(WEBHOOK_PAYLOAD).encode()
    signature = sign_webhook(raw, SECRET)
    tampered = raw.replace(b'"number": 7', b'"number": 8')

    response = post_webhook(client, WEBHOOK_PAYLOAD, signature=signature, body=tampered)

    assert response.status_code == 401
    assert github.calls == []


def test_missing_signature_header_is_rejected(client: TestClient) -> None:
    raw = json.dumps(WEBHOOK_PAYLOAD).encode()
    response = client.post("/webhook", content=raw, headers={"X-GitHub-Event": "pull_request"})
    assert response.status_code == 401


def test_signature_over_reserialised_json_is_rejected(client: TestClient) -> None:
    """Signing anything but the raw bytes must fail — the classic integration bug."""
    raw = json.dumps(WEBHOOK_PAYLOAD, indent=2).encode()
    signature = sign_webhook(json.dumps(WEBHOOK_PAYLOAD, separators=(",", ":")).encode(), SECRET)
    response = post_webhook(client, WEBHOOK_PAYLOAD, signature=signature, body=raw)
    assert response.status_code == 401


# ------------------------------------------------------------------ events


def test_ping_event(client: TestClient, github: FakeGitHub) -> None:
    response = post_webhook(client, {"zen": "Speak like a human."}, event="ping")
    assert response.status_code == 200
    assert response.json()["status"] == "pong"
    assert github.calls == []


def test_unhandled_event_is_ignored(client: TestClient, github: FakeGitHub) -> None:
    response = post_webhook(client, {"issue": {"number": 1}}, event="issues")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert github.calls == []


@pytest.mark.parametrize("action", ["closed", "labeled", "edited", "assigned"])
def test_unhandled_actions_are_ignored(client: TestClient, github: FakeGitHub, action: str) -> None:
    payload = copy.deepcopy(WEBHOOK_PAYLOAD)
    payload["action"] = action

    response = post_webhook(client, payload)

    assert response.status_code == 200
    assert response.json()["reason"] == f"unhandled action {action!r}"
    assert github.calls == [], "GitHub must see a 2xx without us doing any work"


def test_draft_pull_requests_are_ignored(client: TestClient, github: FakeGitHub) -> None:
    payload = copy.deepcopy(WEBHOOK_PAYLOAD)
    payload["pull_request"]["draft"] = True

    response = post_webhook(client, payload)

    assert response.status_code == 200
    assert response.json()["reason"] == "pull request is a draft"
    assert github.calls == []


def test_ready_for_review_runs_even_though_the_flag_lags(
    client: TestClient, github: FakeGitHub
) -> None:
    """GitHub sometimes still reports ``draft: true`` on the ready_for_review event."""
    payload = copy.deepcopy(WEBHOOK_PAYLOAD)
    payload["action"] = "ready_for_review"
    payload["pull_request"]["draft"] = True

    response = post_webhook(client, payload)

    assert response.status_code == 202
    assert github.methods[-1] == "POST"


def test_malformed_json_with_a_valid_signature_is_a_400(client: TestClient) -> None:
    raw = b"{not json"
    response = post_webhook(client, {}, signature=sign_webhook(raw, SECRET), body=raw)
    assert response.status_code == 400
    assert response.json()["reason"] == "body is not valid JSON"


def test_non_object_body_is_a_400(client: TestClient) -> None:
    raw = b"[1, 2, 3]"
    response = post_webhook(client, {}, signature=sign_webhook(raw, SECRET), body=raw)
    assert response.status_code == 400


def test_payload_without_a_pull_request_is_ignored(client: TestClient) -> None:
    response = post_webhook(client, {"action": "opened", "repository": {"full_name": SLUG}})
    assert response.status_code == 200
    assert response.json()["reason"] == "payload carries no pull request"


# --------------------------------------------------------------- settings


def test_from_env_requires_a_webhook_secret() -> None:
    with pytest.raises(RuntimeError, match="MERGESIGNAL_WEBHOOK_SECRET"):
        ServiceSettings.from_env()


def test_from_env_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERGESIGNAL_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("MERGESIGNAL_APP_ID", "123")
    monkeypatch.setenv(
        "MERGESIGNAL_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"
    )

    settings = ServiceSettings.from_env()

    assert settings.webhook_secret == SECRET
    assert settings.app_id == "123"
    assert settings.has_credentials is True


def test_from_env_rejects_a_bad_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERGESIGNAL_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("MERGESIGNAL_RUN_TIMEOUT", "soon")
    with pytest.raises(RuntimeError, match="MERGESIGNAL_RUN_TIMEOUT"):
        ServiceSettings.from_env()


def test_settings_repr_hides_secrets() -> None:
    settings = ServiceSettings(webhook_secret=SECRET, token="ghp_supersecret")
    assert SECRET not in repr(settings)
    assert "ghp_supersecret" not in repr(settings)


def test_create_app_without_settings_needs_the_environment() -> None:
    with pytest.raises(RuntimeError, match="webhook secret"):
        create_app()


# ------------------------------------------------------------- classifying


def test_handled_actions_are_the_documented_ones() -> None:
    assert set(HANDLED_ACTIONS) == {"opened", "synchronize", "reopened", "ready_for_review"}


def test_classify_event_accepts_the_recorded_payload() -> None:
    settings = ServiceSettings(webhook_secret=SECRET)
    assert classify_event(WEBHOOK_PAYLOAD, settings) is None


def test_handle_pull_request_event_short_circuits_on_an_ignored_action() -> None:
    settings = ServiceSettings(webhook_secret=SECRET)
    payload = copy.deepcopy(WEBHOOK_PAYLOAD)
    payload["action"] = "closed"

    assert handle_pull_request_event(payload, settings) == {
        "status": "ignored",
        "reason": "unhandled action 'closed'",
    }


def test_handle_pull_request_event_reports_a_failed_run(settings: ServiceSettings) -> None:
    """A failing analysis returns an error dict rather than raising at GitHub."""
    settings.clone_url = "/nonexistent/repo.git"

    result = handle_pull_request_event(WEBHOOK_PAYLOAD, settings)

    assert result["status"] == "error"
    assert result["repo"] == SLUG
    assert result["pr"] == 7
