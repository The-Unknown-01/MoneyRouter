"""博查（Bocha）Web Search 客户端——按官方 API 实现。

官方（https://open.bochaai.com/）：

- 端点 ``POST {base}/v1/web-search``；
- 鉴权 ``Authorization: Bearer <API-KEY>``；
- 请求体 ``{"query", "freshness", "summary", "count"}``，
  ``freshness`` ∈ noLimit / oneDay / oneWeek / oneMonth / oneYear，``count`` 上限 10；
- 响应 ``data.webPages.value[]``，每项含
  ``name``（标题）/ ``url`` / ``snippet`` / ``summary`` / ``siteName`` / ``datePublished``。

本模块**只负责把材料取回来**，不归纳、不下结论；归纳在 LLM 节点里做。
任何失败都抛 :class:`SearchError`，由节点决定降级——**绝不静默返回占位数据**，
否则"有来源"这条纪律会当场失效。
"""

from __future__ import annotations

import concurrent.futures as futures
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

import httpx

from ..config import SearchSettings
from ..domain.finance import PlannedQuery, classify_source

# 单条材料的片段长度上限（控制喂给模型的上下文体积）
SNIPPET_MAX_CHARS = 600

# --------------------------------------------------------------------------- #
# 失败分类
#
# 区分这两种失败很重要：
#
# - **服务级**（配额 / 鉴权）：不是"这一次没搜到"，而是整条检索线都不可用，
#   需要人去充值或换密钥。此时必须让用户看到"检索服务不可用"，
#   否则会误读成"最近没有相关新闻"。
# - **其余**（网络抖动 / 单条查询异常 / 响应结构变了）：属偶发，重试或跳过即可。
# --------------------------------------------------------------------------- #
KIND_QUOTA = "quota"
KIND_AUTH = "auth"
KIND_NETWORK = "network"
KIND_PROTOCOL = "protocol"
KIND_OTHER = "other"

SERVICE_LEVEL_KINDS = (KIND_QUOTA, KIND_AUTH)

# 面向用户/运维的概括措辞——刻意不含上游原文（log_id 等只进详细消息）
KIND_LABELS: dict[str, str] = {
    KIND_QUOTA: "搜索服务配额或套餐额度不足",
    KIND_AUTH: "搜索服务鉴权失败（API Key 无效或未授权）",
    KIND_NETWORK: "搜索服务网络不可达",
    KIND_PROTOCOL: "搜索服务返回了无法解析的内容",
    KIND_OTHER: "搜索服务调用失败",
}

NO_KEY_LABEL = "未配置搜索服务密钥"

# 判定 403 是"欠费"还是"无权"时的关键词
_QUOTA_HINTS = ("quota", "money", "balance", "insufficient", "arrearage", "expired")


class SearchError(RuntimeError):
    """检索失败（网络、鉴权、配额、响应结构异常）。"""

    def __init__(self, message: str, *, kind: str = KIND_OTHER) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def service_level(self) -> bool:
        """是否需要人工介入（配额耗尽 / 鉴权失败）。"""
        return self.kind in SERVICE_LEVEL_KINDS

    @property
    def label(self) -> str:
        """不含技术细节的概括，可直接展示给用户。"""
        return KIND_LABELS.get(self.kind, KIND_LABELS[KIND_OTHER])


def classify_http(status: int, text: str) -> tuple[str, str]:
    """把 HTTP 状态映射成 ``(kind, 可读说明)``。"""
    lowered = (text or "").lower()
    if status in (401, 407):
        return KIND_AUTH, "API Key 无效或未授权"
    if status == 402:
        return KIND_QUOTA, "配额或套餐额度不足"
    if status == 403:
        if any(hint in lowered for hint in _QUOTA_HINTS):
            return KIND_QUOTA, "配额或套餐额度不足"
        return KIND_AUTH, "无权访问（API Key 无效或未授权）"
    if status == 429:
        return KIND_OTHER, "请求被限流"
    if status >= 500:
        return KIND_NETWORK, "服务端错误"
    return KIND_OTHER, f"HTTP {status}"


def extract_log_id(text: str) -> str:
    """从错误响应里取出 ``log_id``（排查用；取不到就返回空串）。"""
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if isinstance(body, dict):
        for key in ("log_id", "logId", "request_id", "requestId"):
            value = body.get(key)
            if value:
                return str(value)
    return ""


@dataclass(frozen=True)
class SearchHit:
    """一条检索结果，已带上"它是被哪条意图、为哪个方面检索到的"。"""

    query: str
    topic: str
    title: str
    url: str
    site: str = ""
    published_at: str = ""
    snippet: str = ""
    # 来源级别（official / mainstream / general / low）；空串表示尚未判定，
    # 由入库节点用 classify_source 补判
    tier: str = ""


@dataclass(frozen=True)
class SearchOutcome:
    """一次检索批次的结果。

    ``unavailable`` 只在**服务级失败**时给出（配额 / 鉴权 / 未配置密钥），
    它的措辞可直接展示给用户；偶发的单条失败只进 ``errors``。

    实现了 ``__iter__``，因此 ``hits, errors = searcher(...)`` 的老写法仍然可用。
    """

    hits: list[SearchHit]
    errors: list[str]
    unavailable: str | None = None

    def __iter__(self) -> Iterator[Any]:
        return iter((self.hits, self.errors))


Searcher = Callable[[Sequence[PlannedQuery]], Any]


# --------------------------------------------------------------------------- #
# 纯函数：响应解析（单独拎出来便于离线测试）
# --------------------------------------------------------------------------- #
def extract_web_pages(payload: Any) -> list[dict[str, Any]]:
    """从官方响应里取出 ``data.webPages.value``；结构或状态码不对就抛错。"""
    if not isinstance(payload, dict):
        raise SearchError(f"响应不是 JSON 对象：{type(payload).__name__}", kind=KIND_PROTOCOL)

    code = payload.get("code")
    if code is not None and str(code) not in ("200", "0"):
        text = str(payload.get("message") or payload.get("msg") or "")
        numeric = int(code) if str(code).isdigit() else 0
        kind, why = classify_http(numeric, text)
        if kind == KIND_OTHER:
            raise SearchError(f"博查返回错误：code={code} msg={text!r}", kind=KIND_PROTOCOL)
        raise SearchError(f"博查返回错误：{why}（code={code}）", kind=kind)

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise SearchError("响应结构异常：data 不是对象", kind=KIND_PROTOCOL)
    pages = data.get("webPages") or {}
    if not isinstance(pages, dict):
        raise SearchError("响应结构异常：data.webPages 不是对象", kind=KIND_PROTOCOL)

    value = pages.get("value") or []
    if not isinstance(value, list):
        raise SearchError("响应结构异常：data.webPages.value 不是数组", kind=KIND_PROTOCOL)
    return [item for item in value if isinstance(item, dict)]


def _to_hit(item: dict[str, Any], query: str, topic: str) -> SearchHit | None:
    """单条结果 → ``SearchHit``；链接不合法就丢弃（宁缺毋滥）。"""
    url = str(item.get("url") or "").strip()
    if not url.startswith("http"):
        return None
    title = str(item.get("name") or "").strip()
    snippet = str(item.get("summary") or item.get("snippet") or "").strip()
    site = str(item.get("siteName") or "").strip()
    return SearchHit(
        query=query,
        topic=topic,
        title=title or url,
        url=url,
        site=site,
        published_at=str(item.get("datePublished") or "").strip(),
        snippet=" ".join(snippet.split())[:SNIPPET_MAX_CHARS],
        tier=classify_source(site, url),
    )


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class BochaClient:
    """一个薄封装；``transport`` 可注入（测试用 ``httpx.MockTransport``）。"""

    def __init__(
        self, settings: SearchSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=settings.timeout_s, transport=transport)

    def __enter__(self) -> "BochaClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def search(
        self,
        query: str,
        *,
        topic: str = "",
        count: int | None = None,
        freshness: str | None = None,
    ) -> list[SearchHit]:
        """执行一次检索；失败抛 :class:`SearchError`。"""
        payload = {
            "query": query,
            "freshness": freshness or self.settings.freshness,
            "summary": True,
            "count": count or self.settings.count,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.require_api_key()}",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(self.settings.endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError(
                f"请求博查失败：{type(exc).__name__}: {exc}", kind=KIND_NETWORK
            ) from exc

        if response.status_code != 200:
            kind, why = classify_http(response.status_code, response.text)
            detail = f"博查返回 HTTP {response.status_code}：{why}"
            log_id = extract_log_id(response.text)
            if log_id:
                detail += f"（log_id={log_id}）"
            raise SearchError(detail, kind=kind)
        try:
            body = response.json()
        except ValueError as exc:
            raise SearchError("博查返回的不是合法 JSON", kind=KIND_PROTOCOL) from exc

        hits: list[SearchHit] = []
        for item in extract_web_pages(body):
            hit = _to_hit(item, query, topic)
            if hit is not None:
                hits.append(hit)
        return hits


def make_searcher(
    settings: SearchSettings, *, transport: httpx.BaseTransport | None = None
) -> Searcher:
    """返回一个并发检索器：``(queries) -> SearchOutcome``。

    - 多条意图并行发出（线程池），但**结果按意图顺序拼接**，保证可复现；
    - 单条意图失败不影响其余，失败原因收集到 ``errors`` 里由节点写进轨迹；
    - 出现**服务级失败**（配额 / 鉴权）时，把可直接展示的概括写进 ``unavailable``；
    - 未配置密钥时不发请求，直接返回"无材料 + 原因"。
    """

    def search(queries: Sequence[PlannedQuery]) -> SearchOutcome:
        query_list = list(queries)
        if not query_list:
            return SearchOutcome(hits=[], errors=[])
        if settings.degraded:
            return SearchOutcome(
                hits=[],
                errors=[f"{NO_KEY_LABEL}，已跳过联网检索"],
                unavailable=NO_KEY_LABEL,
            )

        hits: list[SearchHit] = []
        errors: list[str] = []
        service_kind: str | None = None
        workers = max(1, min(settings.max_parallel, len(query_list)))
        with BochaClient(settings, transport=transport) as client:
            with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                jobs = [
                    pool.submit(
                        client.search,
                        item.query,
                        topic=item.topic,
                        count=settings.count,
                        freshness=item.freshness,
                    )
                    for item in query_list
                ]
                for item, job in zip(query_list, jobs):
                    try:
                        hits.extend(job.result())
                    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整轮
                        errors.append(f"{item.query} → {type(exc).__name__}: {exc}")
                        if service_kind is None and getattr(exc, "service_level", False):
                            service_kind = getattr(exc, "kind", KIND_OTHER)

        return SearchOutcome(
            hits=hits,
            errors=errors,
            unavailable=KIND_LABELS.get(service_kind) if service_kind else None,
        )

    return search
