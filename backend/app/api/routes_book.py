"""Ledger book routes: transactions, voids, splits, positions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import SplitCreate, TxCreate, VoidCreate
from app.api.serializers import tx_out
from app.core.db import get_db
from app.core.errors import NotFoundError
from app.core.money import s_cash, s_price, s_qty
from app.engines.accounting import build_book
from app.engines.pricing import convert_amount, get_price, value_positions
from app.models.entities import Account, Client, Security
from app.services.ledger import (
    create_split,
    create_transaction,
    void_transaction,
)

router = APIRouter(tags=["book"])


@router.post("/transactions", status_code=201)
def add_transaction(payload: TxCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    tx = create_transaction(
        db,
        account_id=payload.account_id,
        account_code=payload.account_code,
        trade_date=payload.date,
        side=payload.side,
        symbol=payload.symbol,
        quantity=payload.quantity,
        unit_price=payload.unit_price,
        fee=payload.fee,
        fx_rate=payload.fx_rate,
        external_ref=payload.external_ref,
        sequence=payload.sequence,
        currency=payload.currency,
    )
    db.commit()
    db.refresh(tx)
    account = db.get(Account, tx.account_id)
    assert account is not None
    return tx_out(tx, payload.symbol, account.currency)


@router.post("/transactions/{id}/void")
def void_tx(id: str, payload: VoidCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    tx = void_transaction(db, transaction_id=id, reason=payload.reason)
    db.commit()
    db.refresh(tx)
    account = db.get(Account, tx.account_id)
    assert account is not None
    security = db.get(Security, tx.security_id)
    symbol = security.symbol if security else "?"
    return tx_out(tx, symbol, account.currency)


@router.post("/actions/splits", status_code=201)
def add_split(payload: SplitCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    action = create_split(
        db,
        on_date=payload.date,
        symbol=payload.symbol,
        ratio=payload.ratio,
    )
    db.commit()
    db.refresh(action)
    return {
        "id": action.id,
        "symbol": payload.symbol,
        "action_date": action.action_date.isoformat(),
        "ratio": str(action.ratio),
    }


@router.get("/clients/{id}/positions")
def get_client_positions(
    id: str,
    as_of: date | None = Query(None, description="Valuation date, default today"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    client = db.get(Client, id)
    if client is None:
        raise NotFoundError(f"Client '{id}' not found.")
    today = as_of or date.today()
    accounts = db.execute(select(Account).where(Account.client_id == client.id)).scalars().all()
    if not accounts:
        raise NotFoundError(f"No account found for client '{id}'.")

    account = accounts[0]
    book = build_book(db, account.id, today)
    valuation = value_positions(db, book.positions, today, account.currency)
    total_val = valuation.total + book.cash

    positions_list = []
    for sec_id, pos in book.positions.items():
        sec = db.get(Security, sec_id)
        if sec is None:
            continue
        price, carried = get_price(db, sec, today)
        mkt_val = valuation.by_security.get(sec_id, Decimal("0"))
        cost_val = convert_amount(db, pos.quantity * pos.avg_cost, sec.currency, account.currency, today)
        unrealized = mkt_val - cost_val
        weight = (mkt_val / total_val) if total_val else Decimal("0")

        positions_list.append(
            {
                "security_id": sec.id,
                "symbol": sec.symbol,
                "name": sec.name,
                "currency": sec.currency,
                "quantity": s_qty(pos.quantity),
                "avg_cost": s_price(pos.avg_cost),
                "current_price": s_price(price),
                "market_value": s_cash(mkt_val, account.currency),
                "unrealized_gain": s_cash(unrealized, account.currency),
                "weight": f"{weight:.4f}",
                "carried_days": carried,
            }
        )

    positions_list.sort(key=lambda p: p["symbol"])

    return {
        "client_id": client.id,
        "client_code": client.code,
        "account_id": account.id,
        "account_code": account.account_code,
        "currency": account.currency,
        "as_of": today.isoformat(),
        "cash": s_cash(book.cash, account.currency),
        "invested_value": s_cash(valuation.total, account.currency),
        "total_value": s_cash(total_val, account.currency),
        "realized_pnl": s_cash(book.realized, account.currency),
        "max_carried_days": valuation.max_carried_days,
        "positions": positions_list,
    }
