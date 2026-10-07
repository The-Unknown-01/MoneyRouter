"""FastAPI 骨架（预留，本步不启用）。

启用方式::

    ./.venv/Scripts/python.exe -m pip install -e ".[api]"
    ./.venv/Scripts/python.exe -m uvicorn moneyrouter_agent.api.server:app --port 8090

将来 Go 侧通过 ``POST /profile/turn`` 调用（或改为子进程 JSONL 承载）。
本文件刻意不被包 ``__init__`` 引用，避免未装 fastapi 时影响主流程。
"""

from __future__ import annotations

from fastapi import FastAPI

from ..agent import ProfileAgent
from ..month_agent import MonthAgent
from .contract import MonthTurnRequest, MonthTurnResult, TurnRequest, TurnResult

agent = ProfileAgent()
month_agent = MonthAgent()

app = FastAPI(title="MoneyRouter 画像访谈 Agent", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "model": agent.settings.model,
        "degraded": str(agent.settings.degraded),
    }


@app.post("/profile/turn", response_model=TurnResult)
def profile_turn(request: TurnRequest) -> TurnResult:
    """推进一轮对话；停在确认环节时用 ``resume`` 恢复。"""
    return agent.turn(request.thread_id, request.user_message, resume=request.resume)


@app.get("/profile/{thread_id}", response_model=TurnResult)
def profile_snapshot(thread_id: str) -> TurnResult:
    """只读查看某个会话的当前状态。"""
    return agent.snapshot(thread_id)


@app.post("/month/turn", response_model=MonthTurnResult)
def month_turn(request: MonthTurnRequest) -> MonthTurnResult:
    """推进一轮本月核对；停在核对环节时用 ``resume`` 恢复；首轮可带 ``bill`` / ``period``。"""
    return month_agent.turn(
        request.thread_id,
        request.user_message,
        resume=request.resume,
        bill=request.bill,
        period=request.period,
    )


@app.get("/month/{thread_id}", response_model=MonthTurnResult)
def month_snapshot(thread_id: str) -> MonthTurnResult:
    """只读查看某个本月核对会话的当前状态。"""
    return month_agent.snapshot(thread_id)
