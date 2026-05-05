"""Config + secrets loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from aggregator.config import load_config, load_secrets


def test_load_config_validates(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
destination:
  telegram_group: "@dest"
telegram:
  channels: ["@a", -100123]
reddit:
  subreddits: [test]
  poll_interval_seconds: 30
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.destination.telegram_group == "@dest"
    assert cfg.telegram.channels == ["@a", -100123]
    assert cfg.reddit.poll_interval_seconds == 30


def test_load_config_rejects_too_fast_polling(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
destination:
  telegram_group: "@dest"
reddit:
  poll_interval_seconds: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "no.yaml")


def test_load_secrets_requires_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Clear all required env vars first.
    for k in (
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="Missing required"):
        load_secrets(tmp_path / "no.env")


def test_load_secrets_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abcdef")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "rcid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "rcsec")
    monkeypatch.setenv("REDDIT_USER_AGENT", "ua")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    s = load_secrets(tmp_path / "no.env")
    assert s.telegram_api_id == 12345
    assert s.reddit_user_agent == "ua"


def test_load_secrets_rejects_non_int_api_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "not-a-number")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abcdef")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "rcid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "rcsec")
    monkeypatch.setenv("REDDIT_USER_AGENT", "ua")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    with pytest.raises(ValueError, match="must be an integer"):
        load_secrets(tmp_path / "no.env")
