"""Domain errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


class DpmError(Exception):
    code = "dpm_error"
    status = 400

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.extra:
            body["meta"] = self.extra
        return {"error": body}


class NotFoundError(DpmError):
    code = "not_found"
    status = 404


class DuplicateTransactionError(DpmError):
    code = "duplicate_transaction"
    status = 409


class DuplicateFlowError(DpmError):
    code = "duplicate_flow"
    status = 409


class DuplicateActionError(DpmError):
    code = "duplicate_action"
    status = 409


class OverSellError(DpmError):
    code = "oversell"
    status = 409


class ValuationGapError(DpmError):
    code = "valuation_gap"
    status = 422


class FxRateMissingError(DpmError):
    code = "fx_rate_missing"
    status = 422


class PriceUnavailableError(DpmError):
    code = "price_unavailable"
    status = 503


class InfeasiblePlanError(DpmError):
    code = "infeasible_plan"
    status = 422


class PlanExpiredError(DpmError):
    code = "plan_expired"
    status = 409


class PlanStaleError(DpmError):
    code = "plan_stale"
    status = 409


class OrderStateError(DpmError):
    code = "order_state"
    status = 409


class OptimizerError(DpmError):
    code = "optimizer_error"
    status = 503


class SecurityError(DpmError):
    code = "security_error"
    status = 403


class ImportValidationError(DpmError):
    code = "import_validation"
    status = 422
