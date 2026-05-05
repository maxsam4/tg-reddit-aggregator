"""YAML config + .env loader with pydantic validation."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class DestinationConfig(BaseModel):
    telegram_group: str | int


class TelegramConfig(BaseModel):
    channels: list[str | int] = Field(default_factory=list)

    @field_validator("channels")
    @classmethod
    def must_have_channels(cls, v: list[str | int]) -> list[str | int]:
        # Empty is allowed (Reddit-only mode), but we warn at runtime.
        return v


class RedditConfig(BaseModel):
    poll_interval_seconds: int = 60
    posts_per_poll: int = 25
    subreddits: list[str] = Field(default_factory=list)

    @field_validator("poll_interval_seconds")
    @classmethod
    def reasonable_interval(cls, v: int) -> int:
        if v < 10:
            raise ValueError("poll_interval_seconds must be >= 10 to respect Reddit's rate limit")
        return v


class DedupConfig(BaseModel):
    window_hours: int = 24
    max_chars_per_item: int = 1000
    model: str = "claude-haiku-4-5"


class StorageConfig(BaseModel):
    sqlite_path: str = "./data/state.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = "./data/aggregator.log"


class AppConfig(BaseModel):
    destination: DestinationConfig
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    reddit: RedditConfig = Field(default_factory=RedditConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class Secrets(BaseModel):
    """Credentials loaded from .env. Reddit access is unauthenticated; the only
    Reddit-related setting is a custom User-Agent header (REDDIT_USER_AGENT) — it
    has a sensible default if unset, but Reddit blocks generic UAs so set it to
    something identifiable for production."""

    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_path: str = "./data/userbot.session"
    reddit_user_agent: str
    anthropic_api_key: str


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate config.yaml."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to {path} and edit it."
        )
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


def load_secrets(env_path: str | Path = ".env") -> Secrets:
    """Load and validate .env, falling back to actual environment variables."""
    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(env_file)

    def required(name: str) -> str:
        v = os.environ.get(name)
        if not v:
            raise ValueError(f"Missing required environment variable: {name}")
        return v

    api_id_raw = required("TELEGRAM_API_ID")
    try:
        api_id = int(api_id_raw)
    except ValueError as e:
        raise ValueError("TELEGRAM_API_ID must be an integer") from e

    # REDDIT_USER_AGENT is recommended but not strictly required: Reddit will accept
    # a fallback descriptive UA, just less politely.
    from .reddit import DEFAULT_USER_AGENT

    return Secrets(
        telegram_api_id=api_id,
        telegram_api_hash=required("TELEGRAM_API_HASH"),
        telegram_session_path=os.environ.get(
            "TELEGRAM_SESSION_PATH", "./data/userbot.session"
        ),
        reddit_user_agent=os.environ.get("REDDIT_USER_AGENT") or DEFAULT_USER_AGENT,
        anthropic_api_key=required("ANTHROPIC_API_KEY"),
    )
