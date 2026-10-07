"""Private application service. Python domain objects remain the source of truth."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..agent import ProfileAgent
from ..month_agent import MonthAgent
from ..plan_agent import PlanAgent
from ..summary_agent import SummaryAgent
from ..domain.profile import Profile
from ..domain.plan import Plan, PlanInputs
from ..domain.month import SPEND_CATEGORIES
from ..history import JsonMonthHistoryStore
from ..summary_store import JsonExperiencePackStore, JsonProfileEventStore, JsonMonthlySummaryStore

VERSION = "1"
log = logging.getLogger(__name__)

def service_settings():
    from ..config import Settings
    if os.getenv("MONEYROUTER_OFFLINE") == "1":
        return Settings(api_key=None, key_file=None)
    return Settings.from_env()


class Command(BaseModel):
    user_id: str = Field(pattern=r"^u[1-9][0-9]*$")
    kind: str = Field(pattern=r"^(profile|month|finance|plan|summary|chat|clean|manual)$")
    request_id: str = Field(min_length=8, max_length=100)
    period: str = Field(default="", pattern=r"^$|^[0-9]{4}-(0[1-9]|1[0-2])$")
    user_message: str = Field(default="", max_length=2000)
    action: str = Field(default="", pattern=r"^$|^(confirm|edit|more|new|finish)$")
    payload: dict = Field(default_factory=dict)


def public(value):
    """Strip observations, never expose model reasoning or filesystem locations."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k not in {
            "reasoning", "rationale", "trace", "history_location", "write_location",
            "history_warnings", "write_warnings", "event_warnings", "error"}}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


class Service:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "service.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS artifacts(user TEXT,kind TEXT,period TEXT,data TEXT,
            PRIMARY KEY(user,kind,period));
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user TEXT,request TEXT,command TEXT,
            status TEXT,result TEXT,error TEXT,UNIQUE(user,request));
          CREATE TABLE IF NOT EXISTS revisions(user TEXT PRIMARY KEY,value INTEGER DEFAULT 0);
        """)
        self.db.execute("UPDATE jobs SET status='failed',error='服务重启，操作已中断，请核对页面后重试' WHERE status IN ('running','queued')")
        self.db.commit()
        self.lock = threading.RLock()
        self.user_locks = {}
        self.agents = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")
        self.finance = None
        self.finance_lock = threading.Lock()

    def close(self):
        self.pool.shutdown(wait=True)
        for agent in self.agents.values():
            saver = agent.graph.checkpointer
            if hasattr(saver, "conn"):
                saver.conn.close()
        self.db.close()

    def get(self, user, kind, period="", default=None):
        with self.lock:
            row = self.db.execute("SELECT data FROM artifacts WHERE user=? AND kind=? AND period=?", (user, kind, period)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, user, kind, data, period=""):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)", (user, kind, period, json.dumps(data, ensure_ascii=False)))

    def revision(self, user):
        with self.lock:
            row = self.db.execute("SELECT value FROM revisions WHERE user=?", (user,)).fetchone()
            return row[0] if row else 0

    def invalidate(self, user):
        with self.lock, self.db:
            self.db.execute("INSERT INTO revisions VALUES(?,1) ON CONFLICT(user) DO UPDATE SET value=value+1", (user,))

    def user_lock(self, user):
        with self.lock:
            return self.user_locks.setdefault(user, threading.RLock())

    def submit(self, command: Command):
        with self.lock, self.db:
            row = self.db.execute("SELECT id,command FROM jobs WHERE user=? AND request=?", (command.user_id, command.request_id)).fetchone()
            if row:
                if json.loads(row[1]) != command.model_dump():
                    raise ValueError("重复提交标识对应了不同的操作")
                return self.job(command.user_id, row[0])
            count = self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= 32:
                raise ValueError("当前任务较多，请稍后再试")
            if self.db.execute("SELECT 1 FROM jobs WHERE user=? AND status IN ('queued','running')", (command.user_id,)).fetchone():
                raise ValueError("请等待当前操作完成")
            job = uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", (job, command.user_id, command.request_id, command.model_dump_json(), "queued", None, None))
        self.pool.submit(self.run, job, command)
        return self.job(command.user_id, job)

    def job(self, user, job):
        with self.lock:
            row = self.db.execute("SELECT status,result,error FROM jobs WHERE id=? AND user=?", (job, user)).fetchone()
        if not row:
            raise KeyError(job)
        return {"contract_version": VERSION, "job_id": job, "status": row[0], "result": json.loads(row[1]) if row[1] else None, "error": row[2]}

    def run(self, job, command):
        started = time.monotonic()
        with self.user_lock(command.user_id):
            try:
                with self.lock, self.db:
                    self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
                result = self.execute(command)
                with self.lock, self.db:
                    self.db.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?", (json.dumps(public(result), ensure_ascii=False), job))
            except Exception:
                log.exception("agent request failed: request=%s kind=%s", command.request_id, command.kind)
                with self.lock, self.db:
                    self.db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", ("本次操作未完成，请核对当前结果后重试。输入已保留。", job))
            finally:
                log.info("agent request finished: request=%s kind=%s elapsed_ms=%d", command.request_id, command.kind, (time.monotonic()-started)*1000)

    def stores(self, user):
        root = self.root / "users" / user
        return (JsonMonthHistoryStore(root / "months"),
                JsonExperiencePackStore(root / "experience", user=user),
                JsonProfileEventStore(root / "events", user=user),
                JsonMonthlySummaryStore(root / "summaries", user=user))

    def agent(self, user, kind, thread):
        key = (user, kind, thread)
        if key not in self.agents:
            history, pack, events, summaries = self.stores(user)
            # Each graph receives its own durable saver; no mutable process-wide env.
            from ..checkpoints import make_checkpointer
            from ..config import Settings
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
            directory = self.root / "users" / user / "checkpoints"
            directory.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(directory / f"{kind}-{hashlib.sha256(thread.encode()).hexdigest()[:16]}.sqlite", check_same_thread=False)
            # Domain models are explicitly trusted application classes.
            import importlib, inspect
            allowed = []
            for module in ("profile", "month", "probe", "plan", "experience", "summary", "profile_delta", "finance"):
                allowed.extend((v.__module__, v.__name__) for _, v in inspect.getmembers(importlib.import_module(f"moneyrouter_agent.domain.{module}"), inspect.isclass) if issubclass(v, BaseModel))
            allowed.append(("moneyrouter_agent.history", "MonthRecord"))
            saver = SqliteSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=allowed))
            saver.setup()
            if kind == "profile":
                agent = ProfileAgent(settings=service_settings(), checkpointer=saver)
            elif kind == "month":
                agent = MonthAgent(settings=service_settings(), checkpointer=saver, history_store=history)
            elif kind == "plan":
                agent = PlanAgent(settings=service_settings(), checkpointer=saver)
            else:
                agent = SummaryAgent(settings=service_settings(), checkpointer=saver, history_store=history, experience_store=pack, event_store=events, summary_store=summaries)
            self.agents[key] = agent
        return self.agents[key]

    def thread(self, c):
        # Revision creates a fresh conversation when its factual inputs change.
        revision = self.revision(c.user_id)
        if c.kind == "profile":
            revision = self.get(c.user_id, "profile_generation", default=0)
        generation = self.get(c.user_id, "plan_generation", c.period, 0) if c.kind == "plan" else 0
        return f"{c.user_id}:{c.kind}:{c.period}:{revision}:{generation}"

    def execute(self, c):
        user, kind = c.user_id, c.kind
        if kind == "clean":
            return self.clean(c)
        if kind == "manual":
            profile = Profile.model_validate(c.payload)
            # Reject silently sanitized invalid values at this public boundary.
            for name, value in c.payload.items():
                if value is not None and getattr(profile, name, None) is None:
                    raise ValueError("画像字段无效")
            if c.action != "confirm":
                return {"understanding": profile.model_dump(), "awaiting_confirmation": True}
            self.put(user, "profile", profile.model_dump())
            self.put(user, "profile_result", {"confirmed": True, "final_profile": profile.model_dump(), "understanding": profile.model_dump(), "reply": "画像已确认。"})
            self.invalidate(user)
            return self.get(user, "profile_result")
        if kind == "finance":
            return self.briefing(user, c.period)
        if kind == "chat":
            return self.chat(c)
        if kind == "profile" and c.action == "new":
            self.put(user, "profile_generation", self.get(user, "profile_generation", default=0) + 1)
            self.put(user, "profile_result", {})
            self.put(user, "profile", None)
            self.invalidate(user)
        if kind == "plan" and c.action == "new":
            self.put(user, "plan_generation", self.get(user, "plan_generation", c.period, 0)+1, c.period)
        thread = self.thread(c)
        agent = self.agent(user, kind, thread)
        snapshot = agent.snapshot(thread)
        if c.action == "confirm" and not snapshot.awaiting_confirmation:
            if snapshot.confirmed:
                # A crash can happen after checkpoint confirmation but before
                # the display projection is committed. Reconcile it on retry.
                if kind == "month":
                    snapshot = agent._after_turn(snapshot)
                elif kind == "summary":
                    snapshot = agent._after_confirm(snapshot)
                return self.save_result(c, snapshot, thread)
            raise ValueError("当前没有待确认内容")
        if snapshot.awaiting_confirmation and not c.action:
            raise ValueError("必须显式确认或修改")
        resume = {"action": c.action, "message": c.user_message} if c.action in {"confirm", "edit", "more"} else None
        if kind == "profile":
            result = agent.turn(thread, c.user_message, resume=resume)
        elif kind == "month":
            bill = c.payload.get("bill")
            if c.action == "finish":
                if snapshot.turn_count == 0:
                    raise ValueError("请先读取账本")
                result = agent.prepare_confirmation(thread, supplements=c.payload)
            else:
                result = agent.turn(thread, c.user_message, resume=resume, bill=bill, period=c.period)
        elif kind == "plan":
            profile = self.get(user, "profile")
            history, pack, events, summaries = self.stores(user)
            record = history.load(c.period)
            expected = c.payload.get("revision")
            if expected is not None and expected != self.revision(user):
                raise ValueError("输入已更新，请重新生成")
            if not profile or not record or self.get(user, "month_revision", c.period) != self.revision(user):
                raise ValueError("请先确认画像和最新月度实况")
            from ..domain.profile_delta import effective_profile
            effective = effective_profile(Profile.model_validate(profile), events.load(), as_of_period=c.period)
            inputs = PlanInputs(profile=effective.merged(), snapshot=record.snapshot,
                history=[r.snapshot for r in history.load_records() if r.period < c.period],
                briefing=self.briefing(user, c.period)["briefing"], experience=pack.load(), period=c.period,
                as_of=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
            result = agent.plan(thread, inputs=inputs, user_message=c.user_message, resume=resume)
        else:
            history, _, _, _ = self.stores(user)
            if c.period not in history.list_periods():
                raise ValueError("没有已落定的月度记录")
            plan = self.get(user, "confirmed_plan", c.period)
            result = agent.summarize(thread, period=c.period, user_id=user,
                plan=Plan.model_validate(plan) if plan else None, resume=resume)
        unchanged_plan = kind == "plan" and result.error and result.validation and result.validation.ok and (result.awaiting_confirmation or result.confirmed)
        if result.error and not result.degraded and not unchanged_plan:
            raise RuntimeError(result.error)
        return self.save_result(c, result, thread)

    def save_result(self, c, result, thread):
        user, kind = c.user_id, c.kind
        period = "" if kind == "profile" else c.period
        previous = self.get(user, f"{kind}_result", period, {})
        already_confirmed = previous.get("confirmed") and previous.get("thread_id") == thread
        unchanged_plan = kind == "plan" and result.error and result.validation and result.validation.ok and (result.awaiting_confirmation or result.confirmed)
        data = public(result)
        if result.degraded:
            data["service_notice"] = "智能服务暂时降级，已有事实和规则计算仍然保留。"
        if unchanged_plan:
            data["service_notice"] = "修改意见暂时未能解析，已保留原有规则方案。请核对后确认，或稍后重试修改。"
        if kind in {"profile", "month", "plan"}:
            agent = self.agent(user, kind, thread)
            from langchain_core.messages import HumanMessage, AIMessage
            data["messages"] = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": str(m.content)}
                for m in agent.graph.get_state(agent._config(thread)).values.get("messages", []) if isinstance(m, (HumanMessage, AIMessage))][-200:]
        self.put(user, f"{kind}_result", data, "" if kind == "profile" else c.period)
        if kind == "month":
            self.put(user, "month_draft_revision", self.revision(user), c.period)
        if kind == "plan":
            self.put(user, "plan_draft_revision", self.revision(user), c.period)
        if kind == "profile" and result.confirmed:
            self.put(user, "profile", result.final_profile.model_dump())
            if not already_confirmed:
                self.invalidate(user)
        elif kind == "month" and result.confirmed:
            self.put(user, "month_revision", self.revision(user), c.period)
        elif kind == "plan" and result.confirmed:
            if not result.validation or not result.validation.ok:
                raise ValueError("方案未通过独立复核")
            versions = self.get(user, "versions", c.period, [])
            if not versions or versions[-1]["thread_id"] != thread:
                versions.append({"version": len(versions)+1, "thread_id": thread, "revision": self.revision(user), "plan": data["plan"]})
                self.put(user, "versions", versions, c.period)
            self.put(user, "confirmed_plan", data["plan"], c.period)
            self.put(user, "plan_revision", self.revision(user), c.period)
        elif kind == "summary" and result.confirmed:
            if not already_confirmed:
                self.invalidate(user)
        return data

    def briefing(self, user, period):
        cached = self.get(user, "finance_result", period)
        if cached and cached.get("briefing", {}).get("as_of") == datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
            return cached
        from ..finance_agent import FinanceAgent
        from ..domain.finance import FinanceBriefing
        from ..config import Settings
        if service_settings().degraded:
            data = {"briefing": FinanceBriefing(as_of=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(), period=period,
                analysis={"headline": "金融信息暂不可用，本次采用保守规则，不提供市场预测。"}, degraded=True).model_dump(mode="json")}
        else:
            if self.finance is None:
                self.finance = FinanceAgent(settings=service_settings())
            # Finance graph owns mutable code context; serialize this shared cache.
            with self.finance_lock:
                data = public(self.finance.briefing("为个人用户制定审慎的月度收支与财富规划", period=period))
        self.put(user, "finance_result", data, period)
        return data

    def chat(self, c):
        plan = self.get(c.user_id, "confirmed_plan", c.period)
        if not plan:
            raise ValueError("请先确认方案")
        from ..config import Settings
        from ..model.deepseek import build_chat_model
        from langchain_core.messages import SystemMessage, HumanMessage
        if service_settings().degraded:
            reply = "当前为离线模式。请查看方案的金额、风险约束与依据；需要改变计划时请返回方案页提交修改意见。"
        else:
            response = build_chat_model(service_settings()).invoke([
                SystemMessage(content="你是审慎的方案解释助手。只依据所给方案回答；不能变更金额、编造信息或保证收益。不输出思维链。"),
                HumanMessage(content=json.dumps(plan, ensure_ascii=False) + "\n问题：" + c.user_message)])
            reply = str(response.content)
        messages = self.get(c.user_id, "chat_result", c.period, {"messages": []})["messages"]
        messages = (messages + [{"role": "user", "content": c.user_message}, {"role": "assistant", "content": reply}])[-200:]
        data = {"reply": reply, "messages": messages}
        self.put(c.user_id, "chat_result", data, c.period)
        return data

    def clean(self, c):
        from ..tools.bill_cleaner import clean
        from ..tools.dossier import DossierParser
        from ..domain.money import yuan_to_cents
        raw = base64.b64decode(c.payload["content"], validate=True)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("文件过大")
        source = c.payload.get("source", "json")
        warnings = []
        if source in {"alipay", "wechat"}:
            classifier = None
            from ..config import Settings
            if not service_settings().degraded:
                from ..tools.bill_classify import make_deepseek_classifier
                classifier = make_deepseek_classifier(service_settings())
            outcome = clean(**{source: raw}, period=c.period or None, classifier=classifier)
            docs, warnings = outcome.documents, outcome.warnings
        elif source == "json":
            doc = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
            docs = {doc.get("period", c.period): doc}
        else:
            # CSV wide format uses the existing Python parser.
            from ..tools.bills import CSVBillParser
            parsed = CSVBillParser().parse(raw.decode("utf-8-sig"), period=c.period)
            warnings = parsed.warnings
            docs = {c.period: {"period": c.period, "cashflow": [{"date": r.date, "direction": "transfer" if r.excluded else "income" if r.amount_cents > 0 else "expense", "amount": str(Decimal(abs(r.amount_cents))/100), "category": r.category_raw, "note": r.description} for r in parsed.rows]}}
        rows = []
        for period, doc in docs.items():
            parsed = DossierParser().parse(json.dumps(doc, default=str, ensure_ascii=False), period=period)
            warnings.extend(parsed.warnings)
            for row in doc.get("cashflow", []):
                try:
                    from ..tools.dossier import normalize_direction
                    from ..domain.month import normalize_category
                    direction = normalize_direction(row.get("direction"))
                    amount = yuan_to_cents(str(row["amount"]))
                    date = str(row["date"])[:10]
                    datetime.strptime(date, "%Y-%m-%d")
                    if direction is None or amount <= 0 or date[:7] != period:
                        continue
                    category = normalize_category(str(row.get("category", "其他"))) if direction == "expense" else str(row.get("category", "其他收入"))
                    item = {"date": date, "direction": direction, "amount_cents": amount, "category": category,
                        "note": str(row.get("note", "")), "source": source, "source_id": str(row.get("transaction_id", ""))}
                    item["fingerprint"] = hashlib.sha256(json.dumps([source, item["source_id"] or [date, direction, amount, category, item["note"]]], ensure_ascii=False).encode()).hexdigest()
                    rows.append(item)
                except (KeyError, ValueError, TypeError):
                    warnings.append("部分流水格式无效，已跳过，请核对预览。")
        if len(rows) > 5000:
            raise ValueError("单次最多导入 5000 笔")
        # Decimal amounts stay exact as decimal strings across JSON.
        return {"rows": rows, "documents": json.loads(json.dumps(docs, default=str)), "warnings": warnings,
            "periods": sorted(docs), "count": len(rows)}

    def state(self, user, period):
        history, pack, events, summaries = self.stores(user)
        with self.lock:
            data = {"contract_version": VERSION, "period": period, "revision": self.revision(user),
                "categories": list(SPEND_CATEGORIES), "history_periods": history.list_periods(),
                "profile": self.get(user, "profile"), "profile_result": self.get(user, "profile_result", default={}),
                "versions": self.get(user, "versions", period, []),
                "stale": bool(self.get(user, "plan_result", period)) and self.get(user, "plan_draft_revision", period, self.get(user, "plan_revision", period)) != self.revision(user),
                "month_current": self.get(user, "month_revision", period) == self.revision(user)}
            data["month_draft_stale"] = bool(self.get(user, "month_result", period)) and self.get(user, "month_draft_revision", period) != self.revision(user)
            for kind in ("month", "plan", "summary", "finance", "chat"):
                data[f"{kind}_result"] = self.get(user, f"{kind}_result", period, {})
            data["history"] = [public(r) for r in history.load_records()]
            with self.lock:
                pending = self.db.execute("SELECT id,command FROM jobs WHERE user=? AND status IN ('queued','running') ORDER BY rowid DESC LIMIT 1", (user,)).fetchone()
            data["pending"] = {"job_id": pending[0], "kind": json.loads(pending[1])["kind"]} if pending else None
            return public(data)
