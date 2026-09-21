"""Tests for ``.mergesignal.yaml`` loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mergesignal.config import (
    RISK_FACTORS,
    Config,
    ConfigError,
    RiskWeights,
    default_config,
    find_config_file,
    load_config,
    match_any,
)
from mergesignal.models import SIGNAL_NAMES


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(repo_path=tmp_path)
    assert config == default_config()
    assert config.enabled_signals == list(SIGNAL_NAMES)
    assert config.severity_threshold == "high"
    assert config.source_path is None


def test_explicit_missing_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_loads_full_config(write_config, tmp_path: Path) -> None:
    path = write_config(
        """
enabled_signals: [risk, conflicts]
severity_threshold: medium
risk_weights:
  churn: 0.5
  co_change: 0.1
  hot_paths: 0.2
  test_coverage: 0.1
  diff_size: 0.1
hot_paths:
  - "src/**/models.py"
github:
  repo: acme/widgets
  comment: false
"""
    )
    config = load_config(path)
    assert config.enabled_signals == ["conflicts", "risk"]
    assert config.severity_threshold == "medium"
    assert config.risk_weights.churn == 0.5
    assert config.github.repo == "acme/widgets"
    assert config.github.comment is False
    assert config.source_path == str(path.resolve())
    assert Path(config.source_path).parent == tmp_path


def test_empty_file_is_all_defaults(write_config) -> None:
    config = load_config(write_config(""))
    assert config.enabled_signals == list(SIGNAL_NAMES)


def test_non_mapping_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write_config("- just\n- a list\n"))


def test_invalid_yaml_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write_config("key: [unclosed\n"))


def test_unknown_key_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(write_config("hot_path: [a]\n"))


def test_unknown_signal_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="unknown signals"):
        load_config(write_config("enabled_signals: [conflict]\n"))


def test_zero_weights_are_rejected(write_config) -> None:
    body = "risk_weights:\n" + "".join(f"  {name}: 0\n" for name in RISK_FACTORS)
    with pytest.raises(ConfigError):
        load_config(write_config(body))


def test_bad_repo_slug_is_rejected(write_config) -> None:
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(write_config("github:\n  repo: acme\n"))


def test_signals_are_canonically_ordered() -> None:
    config = Config(enabled_signals=["risk", "semantic", "conflicts"])
    assert config.enabled_signals == ["conflicts", "semantic", "risk"]
    assert config.is_enabled("risk")
    assert not config.is_enabled("overlap")


def test_risk_weights_helpers() -> None:
    weights = RiskWeights()
    assert set(weights.as_dict()) == set(RISK_FACTORS)
    assert weights.total == pytest.approx(sum(weights.as_dict().values()))


def test_find_config_file_walks_up(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".mergesignal.yaml").write_text("severity_threshold: low\n", encoding="utf-8")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    found = find_config_file(nested)
    assert found == tmp_path / ".mergesignal.yaml"
    assert load_config(repo_path=nested).severity_threshold == "low"


def test_find_config_file_stops_at_repo_root(tmp_path: Path) -> None:
    (tmp_path / ".mergesignal.yaml").write_text("severity_threshold: low\n", encoding="utf-8")
    inner = tmp_path / "inner"
    (inner / ".git").mkdir(parents=True)
    assert find_config_file(inner) is None


def test_yml_extension_is_accepted(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".mergesignal.yml").write_text("severity_threshold: critical\n", encoding="utf-8")
    assert load_config(repo_path=tmp_path).severity_threshold == "critical"


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("src/mergesignal/models.py", "src/**/models.py", True),
        ("src/models.py", "src/*.py", True),
        ("db/migrations/001.sql", "*.sql", True),
        ("src/app.py", "tests/**", False),
        ("src/app.py", "", False),
    ],
)
def test_match_any(path: str, pattern: str, expected: bool) -> None:
    assert match_any(path, [pattern]) is expected


def test_hot_and_ignored_paths() -> None:
    config = Config(hot_paths=["src/**/models.py"], ignore_paths=["vendor/**"])
    assert config.is_hot("src/mergesignal/models.py")
    assert not config.is_hot("src/other.py")
    assert config.is_ignored("vendor/lib/x.py")


def test_github_token_read_from_named_env(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config()
    assert config.github.token() is None
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert config.github.token() == "secret"
