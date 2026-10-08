"""博查客户端：请求形状、响应解析、错误处理、并发检索的降级。

全部离线——用 ``httpx.MockTransport`` 顶替真实网络。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from moneyrouter_agent.config import SearchSettings
from moneyrouter_agent.domain.finance import PlannedQuery
from moneyrouter_agent.tools.bocha import (
    BochaClient,
    SearchError,
    extract_web_pages,
    make_searcher,
)


def _payload(items: list[dict[str, Any]], code: int | str = 200) -> dict[str, Any]:
    return {"code": code, "log_id": "0d0e", "msg": None, "data": {"webPages": {"value": items}}}


def _item(url: str = "https://a.example/1", **extra: Any) -> dict[str, Any]:
    base = {
        "name": "网页标题",
        "url": url,
        "snippet": "简短描述",
        "summary": "较长摘要",
        "siteName": "示例站点",
        "datePublished": "2026-10-01T00:00:00+08:00",
    }
    base.update(extra)
    return base


def _settings(**overrides: Any) -> SearchSettings:
    return SearchSettings(api_key="k", **overrides)


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #
def test_extract_web_pages_reads_official_shape():
    items = extract_web_pages(_payload([_item()]))
    assert items[0]["name"] == "网页标题"


def test_extract_web_pages_rejects_error_code():
    with pytest.raises(SearchError) as exc:
        extract_web_pages(_payload([], code=403))
    assert "403" in str(exc.value)


def test_extract_web_pages_rejects_broken_structure():
    with pytest.raises(SearchError):
        extract_web_pages({"code": 200, "data": {"webPages": {"value": "不是数组"}}})
    with pytest.raises(SearchError):
        extract_web_pages("不是对象")


# --------------------------------------------------------------------------- #
# 请求形状与解析
# --------------------------------------------------------------------------- #
def test_client_sends_official_request_shape():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_payload([_item()]))

    with BochaClient(_settings(count=5), transport=httpx.MockTransport(handler)) as client:
        hits = client.search("黄金价格", topic="跨资产行情", freshness="oneWeek")

    request = seen[0]
    assert str(request.url) == "https://api.bochaai.com/v1/web-search"
    assert request.headers["Authorization"] == "Bearer k"
    body = json.loads(request.content)
    assert body == {"query": "黄金价格", "freshness": "oneWeek", "summary": True, "count": 5}

    assert len(hits) == 1
    assert hits[0].title == "网页标题"
    assert hits[0].topic == "跨资产行情"
    assert hits[0].query == "黄金价格"
    assert hits[0].site == "示例站点"
    assert hits[0].published_at.startswith("2026-10-01")


def test_client_drops_items_without_usable_url():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload([_item(url=""), _item(url="javascript:void(0)"), _item(url="https://ok.example/1")]),
        )

    with BochaClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        hits = client.search("x")

    assert [hit.url for hit in hits] == ["https://ok.example/1"]


def test_client_raises_on_non_200():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    with BochaClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SearchError) as exc:
            client.search("x")
    assert "429" in str(exc.value)
    # 限流属偶发，不是"服务级失败"，不该触发"请去充值"的提示
    assert exc.value.kind == "other"
    assert exc.value.service_level is False


def test_quota_exhaustion_is_classified_as_service_level():
    """博查配额耗尽返回 403 + 配额文案——必须与"无权访问"区分开。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "message": "You do not have enough money or package quota",
                "log_id": "abc123",
                "code": "403",
            },
        )

    with BochaClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SearchError) as exc:
            client.search("x")

    error = exc.value
    assert error.kind == "quota"
    assert error.service_level is True
    assert error.label == "搜索服务配额或套餐额度不足"
    # 详细消息里带 log_id 便于排查，但不含密钥
    assert "abc123" in str(error)
    assert _settings().api_key not in str(error)


def test_invalid_key_is_classified_as_auth():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid api key"})

    with BochaClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SearchError) as exc:
            client.search("x")

    assert exc.value.kind == "auth"
    assert exc.value.service_level is True


def test_client_without_key_refuses_to_call():
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没有密钥时不应该发出请求")

    with BochaClient(SearchSettings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            client.search("x")


# --------------------------------------------------------------------------- #
# 并发检索器
# --------------------------------------------------------------------------- #
def _query(text: str, topic: str = "宏观与市场环境") -> PlannedQuery:
    return PlannedQuery(query=text, topic=topic)


def test_searcher_skips_network_without_key():
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没有密钥时不应该发出请求")

    searcher = make_searcher(SearchSettings(), transport=httpx.MockTransport(handler))
    outcome = searcher([_query("a")])

    assert outcome.hits == []
    assert outcome.errors and "未配置搜索服务密钥" in outcome.errors[0]
    assert outcome.unavailable == "未配置搜索服务密钥"
    # 解包写法仍然可用（老调用方不受影响）
    hits, errors = outcome
    assert hits == [] and errors


def test_searcher_keeps_query_order_and_survives_partial_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = body["query"]
        if "boom" in text:
            raise httpx.ConnectError("连接失败")
        return httpx.Response(200, json=_payload([_item(url=f"https://a.example/{text}")]))

    searcher = make_searcher(_settings(), transport=httpx.MockTransport(handler))
    hits, errors = searcher([_query("甲"), _query("boom"), _query("丙")])

    # 结果按意图顺序拼接（与并发完成顺序无关）
    assert [hit.query for hit in hits] == ["甲", "丙"]
    assert len(errors) == 1 and "boom" in errors[0]


def test_searcher_reports_quota_failure_as_unavailable():
    """配额耗尽是服务级失败：要给出可展示的概括，而不是让人以为"最近没新闻"。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "message": "You do not have enough money or package quota",
                "log_id": "1f7a",
                "code": "403",
            },
        )

    searcher = make_searcher(_settings(), transport=httpx.MockTransport(handler))
    outcome = searcher([_query("甲")])

    assert outcome.hits == []
    assert outcome.unavailable == "搜索服务配额或套餐额度不足"
    assert "403" in outcome.errors[0]


def test_searcher_empty_input_makes_no_call():
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("空输入不应发请求")

    searcher = make_searcher(_settings(), transport=httpx.MockTransport(handler))
    outcome = searcher([])

    assert outcome.hits == [] and outcome.errors == []
    assert outcome.unavailable is None


def test_searcher_carries_topic_from_planned_query():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([_item()]))

    searcher = make_searcher(_settings(), transport=httpx.MockTransport(handler))
    hits, _ = searcher([_query("金价", topic="跨资产行情")])

    assert hits[0].topic == "跨资产行情"
