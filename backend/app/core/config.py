"""Runtime configuration (DPM_* environment variables)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DPM_",
        env_file=str(ROOT / ".env"),
        extra="ignore",
        case_sensitive=False,
    )

    db_path: str = "./data/dpm.db"
    bind: str = "127.0.0.1"
    port: int = 8000
    local_token: str = ""
    price_ttl: int = 300
    plan_ttl: int = 900
    slippage_cap: Decimal = Decimal("0.02")
    dust_value: Decimal = Decimal("100")
    lot_epsilon: Decimal = Decimal("0.00000001")
    est_fee_bps: Decimal = Decimal("10")
    default_settlement_days: int = 2
    backup_dir: str = "./data/backups"
    data_provider: str = "yahoo"
    optimizer_enabled: bool = False
    carry_forward_max: int = 5
    allowed_hosts: str = "127.0.0.1,localhost,testserver"

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

    @property
    def backup_folder(self) -> Path:
        p = Path(self.backup_dir)
        return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

    @property
    def hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}


settings = Settings()
