"""Configuration resolution, and the guarantee that nothing is hardcoded."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import (
    ConfigError,
    Settings,
    get_settings,
    redact_database_url,
)

DB_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Isolate settings resolution: clear DB env and the settings cache."""
    for key in DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_database_url_is_required(clean_env):
    with pytest.raises(ConfigError) as excinfo:
        get_settings()
    assert "DATABASE_URL" in str(excinfo.value)


def test_url_assembled_from_discrete_parts(clean_env):
    clean_env.setenv("DATABASE_HOST", "db.example.test")
    clean_env.setenv("DATABASE_PORT", "6543")
    clean_env.setenv("DATABASE_NAME", "appdb")
    clean_env.setenv("DATABASE_USER", "appuser")
    clean_env.setenv("DATABASE_PASSWORD", "p@ss word/1")

    settings = get_settings()
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert "db.example.test:6543" in settings.database_url
    # Special characters must be percent-encoded, not left to break the URL.
    assert "p%40ss+word%2F1" in settings.database_url
    assert settings.safe_database_target == "db.example.test:6543"


def test_incomplete_discrete_config_names_what_is_missing(clean_env):
    clean_env.setenv("DATABASE_HOST", "db.example.test")
    clean_env.setenv("DATABASE_NAME", "appdb")
    with pytest.raises(ConfigError) as excinfo:
        get_settings()
    message = str(excinfo.value)
    assert "DATABASE_USER" in message
    assert "DATABASE_PASSWORD" in message


def test_invalid_failure_mode_is_rejected(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    clean_env.setenv("DB_FAILURE_MODE", "explode")
    with pytest.raises(ConfigError):
        get_settings()


def test_bounds_are_clamped_to_safe_values(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    clean_env.setenv("CPU_STRESS_MAX_DURATION", "0")
    clean_env.setenv("SLOW_QUERY_MAX_SECONDS", "-10")
    clean_env.setenv("SLOW_QUERY_DEFAULT_SECONDS", "9999")

    settings = get_settings()
    assert settings.cpu_stress_max_duration >= 1
    assert settings.slow_query_max_seconds >= 1
    # A default larger than the cap must never win.
    assert settings.slow_query_default_seconds <= settings.slow_query_max_seconds


def test_redaction_hides_credentials():
    redacted = redact_database_url("postgresql://admin:s3cret@db.internal:5432/appdb")
    assert "s3cret" not in redacted
    assert "admin" not in redacted
    assert "db.internal:5432" in redacted


def test_settings_repr_excludes_the_database_url(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql://admin:s3cret@db.internal:5432/appdb")
    settings = get_settings()
    assert "s3cret" not in repr(settings)
    assert isinstance(settings, Settings)


def test_no_credentials_are_hardcoded_in_the_source_tree():
    """Guard against a real DSN being pasted into the app package.

    Matches ``scheme://user:password@host`` while ignoring interpolated
    templates (where a ``{`` follows the scheme) and driver-prefix constants.
    """
    dsn_with_credentials = re.compile(r"://(?![{\"'])[^\s\"'{}/]+:[^\s\"'{}/@]+@")
    app_dir = Path(__file__).resolve().parents[2] / "app"
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not dsn_with_credentials.search(
            text
        ), f"possible hardcoded connection string with credentials in {path.name}"
