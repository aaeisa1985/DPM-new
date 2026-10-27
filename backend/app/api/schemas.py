"""Pydantic request models. Money inputs are accepted as strings or numbers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.money import D


def _valid_date(value: date) -> date:
    return value


class ClientCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    display_name: str = Field(min_length=1, max_length=160)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("code", "display_name")
    @classmethod
    def strip_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("base_currency")
    @classmethod
    def upper_ccy(cls, v: str) -> str:
        return v.upper()


class MandateCreate(BaseModel):
    client_id: str
    name: str = Field(min_length=1, max_length=160)
    status: Literal["active", "closed"] = "active"


class VersionCreate(BaseModel):
    effective_from: date
    drift_tolerance: Decimal = Decimal("0.05")
    lot_method: Literal["fifo", "hifo"] = "fifo"
    benchmark_symbol: str | None = None
    weights: dict[str, Decimal]

    @model_validator(mode="after")
    def check_weights(self) -> VersionCreate:
        cleaned = {k.strip().upper(): D(v) for k, v in self.weights.items()}
        if "CASH" not in cleaned:
            raise ValueError("weights must include an explicit CASH key")
        total = sum(cleaned.values(), Decimal("0"))
        if abs(total - Decimal("1")) > Decimal("0.00000001"):
            raise ValueError(f"weights must sum to exactly 1.0 (got {total})")
        if any(v < 0 for v in cleaned.values()):
            raise ValueError("weights cannot be negative")
        self.weights = cleaned
        return self


class SecurityCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=160)
    yahoo_ticker: str | None = None
    asset_class: str = "equity"
    currency: str = "USD"
    price_source: Literal["manual", "yahoo", "openbb"] = "manual"
    price_unit: Literal["share", "subunit"] = "share"
    divisor: Decimal = Decimal("1")
    settlement_days: int = 2
    manual_price: Decimal | None = None

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("currency")
    @classmethod
    def upper_ccy(cls, v: str) -> str:
        return v.strip().upper()


class AccountCreate(BaseModel):
    account_code: str = Field(min_length=1, max_length=40)
    client_id: str
    mandate_id: str
    currency: str = "USD"
    opening_cash: Decimal = Decimal("0")

    @field_validator("account_code")
    @classmethod
    def upper_code(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("currency")
    @classmethod
    def upper_ccy(cls, v: str) -> str:
        return v.strip().upper()


class FlowCreate(BaseModel):
    flow_date: date
    type: Literal["deposit", "withdrawal", "dividend", "fee"]
    amount: Decimal
    external_ref: str | None = None
    security_id: str | None = None
    sequence: int | None = None

    @model_validator(mode="after")
    def check_sign(self) -> FlowCreate:
        if self.type in ("deposit", "dividend") and self.amount < 0:
            raise ValueError("deposit/dividend amounts must be positive")
        if self.type in ("withdrawal", "fee") and self.amount > 0:
            self.amount = -abs(self.amount)
        if self.type == "dividend" and not self.security_id:
            raise ValueError("security_id is required for dividends")
        return self


class DividendCreate(BaseModel):
    date: date
    security_id: str
    per_unit: Decimal
    external_ref: str | None = None


class FxCreate(BaseModel):
    date: date
    pair: str = Field(min_length=6, max_length=6)
    rate: Decimal

    @field_validator("pair")
    @classmethod
    def upper_pair(cls, v: str) -> str:
        return v.strip().upper()


class PriceCreate(BaseModel):
    date: date
    symbol: str
    close: Decimal
    currency: str | None = None

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class TxCreate(BaseModel):
    account_code: str | None = None
    account_id: str | None = None
    date: date
    side: Literal["BUY", "SELL"]
    symbol: str
    quantity: Decimal
    unit_price: Decimal
    fee: Decimal = Decimal("0")
    currency: str | None = None
    fx_rate: Decimal | None = None
    external_ref: str | None = None
    sequence: int | None = None

    @model_validator(mode="after")
    def check(self) -> TxCreate:
        if not self.account_code and not self.account_id:
            raise ValueError("account_code or account_id is required")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.unit_price < 0 or self.fee < 0:
            raise ValueError("price and fee cannot be negative")
        self.symbol = self.symbol.strip().upper()
        return self


class SplitCreate(BaseModel):
    date: date
    symbol: str
    ratio: Decimal

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def check_ratio(self) -> SplitCreate:
        if self.ratio <= 0:
            raise ValueError("ratio must be positive")
        return self


class VoidCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=240)


class FillCreate(BaseModel):
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")

    @model_validator(mode="after")
    def check(self) -> FillCreate:
        if self.quantity <= 0 or self.price < 0 or self.fee < 0:
            raise ValueError("invalid fill values")
        return self


class CancelCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=240)


class ImportRequest(BaseModel):
    csv_content: str
    dry_run: bool = True


class OptimizeRequest(BaseModel):
    symbols: list[str] | None = None
    lookback_days: int = 730


class JsonModel(BaseModel):
    """Base for loose JSON responses."""

    def model_post_init(self, __context: Any) -> None:
        pass
