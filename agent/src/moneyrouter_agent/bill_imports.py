"""Transactional per-user bill ledger: repeat uploads replace neither sums nor facts."""
from __future__ import annotations
import calendar
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .tools.bill_cleaner import RawRow, build_documents, clean


class BillImportStore:
    def __init__(self, path: str | Path, *, user: str):
        if not user.strip():
            raise ValueError("账单导入必须指定用户")
        self.user = user
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS transactions(user TEXT, source TEXT, id TEXT, payload TEXT,
                    PRIMARY KEY(user,source,id));
                CREATE TABLE IF NOT EXISTS uploads(user TEXT, digest TEXT, PRIMARY KEY(user,digest));
                CREATE TABLE IF NOT EXISTS coverage(user TEXT, source TEXT, start TEXT, end TEXT,
                    PRIMARY KEY(user,source,start,end));
            """)

    def has_upload(self, digest):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT 1 FROM uploads WHERE user=? AND digest=?", (self.user, digest)).fetchone() is not None

    def fully_classified(self, period=None):
        with sqlite3.connect(self.path) as conn:
            rows = [RawRow(**json.loads(r[0])) for r in conn.execute("SELECT payload FROM transactions WHERE user=?", (self.user,))]
        return all(r.category_source in ("model", "user") for r in rows if r.direction == "expense" and (period is None or r.period == period))

    def import_outcome(self, outcome, *, digest: str, file_digests=None):
        added = duplicate = updated = 0
        with sqlite3.connect(self.path, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in outcome.rows:
                key = row.transaction_id or f"file:{(file_digests or {}).get(row.source, digest)}:row:{row.source_row}"
                old = conn.execute("SELECT payload FROM transactions WHERE user=? AND source=? AND id=?",
                                   (self.user, row.source, key)).fetchone()
                if old:
                    previous = RawRow(**json.loads(old[0]))
                    # Transaction status and user classification can evolve; monetary facts cannot silently change.
                    if (previous.date, previous.direction, previous.amount_cents) != (row.date, row.direction, row.amount_cents):
                        raise ValueError("同一交易编号的日期、方向或金额冲突，整次导入已回滚")
                    if ((previous.category_source == "user" and row.category_source != "user") or
                            (previous.category_source == "model" and row.category_source in ("rule", "unknown"))):
                        from dataclasses import replace
                        row = replace(row, category=previous.category, category_source=previous.category_source, review_reason=previous.review_reason)
                    if asdict(previous) == asdict(row):
                        duplicate += 1
                        continue
                    updated += 1
                else:
                    added += 1
                conn.execute("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?)",
                             (self.user, row.source, key, json.dumps(asdict(row), ensure_ascii=False)))
            for source, (start, end) in outcome.coverage_ranges.items():
                conn.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?,?)", (self.user, source, start, end))
            conn.execute("INSERT OR IGNORE INTO uploads VALUES (?,?)", (self.user, digest))
        return {"added": added, "duplicate": duplicate, "updated": updated}

    def documents(self, *, refund_policy="net", period=None):
        with sqlite3.connect(self.path) as conn:
            rows = [RawRow(**json.loads(r[0])) for r in conn.execute(
                "SELECT payload FROM transactions WHERE user=? ORDER BY source,id", (self.user,))]
            coverage = list(conn.execute("SELECT source,start,end FROM coverage WHERE user=? ORDER BY source,start", (self.user,)))
        intervals = {}
        for source, start, end in coverage:
            group = intervals.setdefault(source, [])
            if group and date.fromisoformat(start) <= date.fromisoformat(group[-1][1]) + timedelta(days=1):
                group[-1][1] = max(group[-1][1], end)
            else:
                group.append([start, end])
        documents = build_documents(rows, refund_policy=refund_policy, period=period)
        for month, doc in documents.items():
            first = month + "-01"
            last = month + f"-{calendar.monthrange(int(month[:4]), int(month[5:]))[1]:02d}"
            sources = {r.source for r in rows if r.period == month}
            known = all(s in intervals for s in sources)
            starts = [min(a for a,b in intervals[s]) for s in sources if s in intervals]
            ends = [max(b for a,b in intervals[s]) for s in sources if s in intervals]
            doc["coverage"] = {"start": max([first] + starts), "end": min([last] + ends),
                "complete": all(any(a <= first and b >= last for a,b in intervals[s]) for s in sources) if known else None,
                "source_ranges": {s: intervals.get(s, []) for s in sources}}
        return documents


def import_bills(store: BillImportStore, *, alipay=None, wechat=None, **kwargs):
    # Hash each file independently, so adding another source does not change no-ID replay identity.
    file_digests = {}
    for source, value in (("alipay", alipay), ("wechat", wechat)):
        if value is None:
            continue
        data = value if isinstance(value, bytes) else (str(value).encode() if source == "alipay" else Path(value).read_bytes())
        file_digests[source] = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(json.dumps(file_digests, sort_keys=True).encode()).hexdigest()
    if store.has_upload(digest) and store.fully_classified(kwargs.get("period")):
        kwargs["classifier"] = None
    outcome = clean(alipay=alipay, wechat=wechat, **kwargs)
    stats = store.import_outcome(outcome, digest=digest, file_digests=file_digests)
    return outcome, store.documents(refund_policy=kwargs.get("refund_policy", "net"), period=kwargs.get("period")), stats
