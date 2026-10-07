"""对外集成层（契约 + 预留服务骨架）。"""

from .contract import (
    FinanceBriefingResponse,
    FinanceBriefingResult,
    FinanceContextRequest,
    TurnRequest,
    TurnResult,
)

__all__ = [
    "FinanceBriefingResponse",
    "FinanceBriefingResult",
    "FinanceContextRequest",
    "TurnRequest",
    "TurnResult",
]
