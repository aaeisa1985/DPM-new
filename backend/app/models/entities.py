"""SQLAlchemy models (schema v1.1). All money/quantity columns are TEXT."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.money import Money


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mandates: Mapped[list[Mandate]] = relationship(back_populates="client")


class Mandate(Base):
    __tablename__ = "mandates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    client: Mapped[Client] = relationship(back_populates="mandates")
    versions: Mapped[list[MandateVersion]] = relationship(
        back_populates="mandate", order_by="MandateVersion.version"
    )


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    mandate_id: Mapped[str] = mapped_column(
        ForeignKey("mandates.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    opening_cash: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MandateVersion(Base):
    __tablename__ = "mandate_versions"
    __table_args__ = (UniqueConstraint("mandate_id", "version", name="uq_mandate_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    drift_tolerance: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0.05"))
    lot_method: Mapped[str] = mapped_column(String(8), nullable=False, default="fifo")
    benchmark_id: Mapped[str | None] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"), nullable=True
    )
    weights_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mandate: Mapped[Mandate] = relationship(back_populates="versions")


class Security(Base):
    __tablename__ = "securities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    yahoo_ticker: Mapped[str | None] = mapped_column(String(60), nullable=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(40), nullable=False, default="equity")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    price_source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual")
    price_unit: Mapped[str] = mapped_column(String(10), nullable=False, default="share")
    divisor: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("1"))
    settlement_days: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    manual_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_tx_idem"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    external_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    fx_rate: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("1"))
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active")
    void_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CashFlow(Base):
    __tablename__ = "cash_flows"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_flow_idem"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    external_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    flow_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    security_id: Mapped[str | None] = mapped_column(ForeignKey("securities.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_action_idem"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), nullable=False)
    action_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="split")
    ratio: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceHistory(Base):
    __tablename__ = "price_history"
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FxHistory(Base):
    __tablename__ = "fx_history"
    pair: Mapped[str] = mapped_column(String(8), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual")


class PriceCache(Base):
    __tablename__ = "price_cache"
    security_id: Mapped[str] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="yahoo")


class RebalancePlan(Base):
    __tablename__ = "rebalance_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    snapshot_last_tx_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    snapshot_last_tx_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_last_flow_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    snapshot_last_flow_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cash_after: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cash_target: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    est_fee_bps: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("10"))
    orders: Mapped[list[RebalanceOrder]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="RebalanceOrder.created_at"
    )


class RebalanceOrder(Base):
    __tablename__ = "rebalance_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("rebalance_plans.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[str] = mapped_column(ForeignKey("securities.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    estimated_price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    filled_quantity: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    filled_price: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    filled_fee: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    plan: Mapped[RebalancePlan] = relationship(back_populates="orders")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    actor: Mapped[str] = mapped_column(String(60), nullable=False, default="local-operator")
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
