"""Service configuration. Env-only (``CAREGAP_`` prefix, ``.env``), never committed."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Configuration missing for the requested operation (e.g. no key for anthropic mode)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREGAP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Models: real Anthropic only in explicit local runs; CI never sets a key.
    models: Literal["fake", "replay", "anthropic"] = "replay"
    anthropic_api_key: SecretStr | None = None
    validator_model: str = "claude-sonnet-5"
    drafter_model: str = "claude-sonnet-5"
    judge_model: str = "claude-opus-5"

    # P6 access.
    p6_mode: Literal["embedded", "http", "snapshot"] = "embedded"
    p6_url: str = "http://127.0.0.1:8000"
    p6_db_path: Path = Path("data/p6.duckdb")
    snapshot_dir: Path = Path("synthetic/p6_snapshots")

    # Runtime.
    checkpoint_path: Path = Path("data/checkpoints.sqlite")
    runstore_path: Path = Path("data/caregap.sqlite")
    recordings_dir: Path = Path("evals/recorded")
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    patient_timeout_s: float = 120.0
    max_patients_per_run: int = 50

    # Drafter context.
    clinic_name: str = "Demo Primary Care"
    clinic_phone: str = "555-0100"


def get_settings() -> Settings:
    return Settings()
