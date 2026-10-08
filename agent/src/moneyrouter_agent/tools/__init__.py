"""外部数据源适配层：联网检索 + 结构化行情/宏观数据 + 账单/规范文档解析。"""

from .bills import BillParser, ParsedBill, looks_like_json, parse_bill, register_parser
from .bocha import BochaClient, SearchError, SearchHit, Searcher, make_searcher
from .dossier import DossierParser, sample_document, sample_dossier
from .market_data import MarketDataClient, clear_cache

__all__ = [
    "BillParser",
    "BochaClient",
    "DossierParser",
    "MarketDataClient",
    "ParsedBill",
    "SearchError",
    "SearchHit",
    "Searcher",
    "clear_cache",
    "looks_like_json",
    "make_searcher",
    "parse_bill",
    "register_parser",
    "sample_document",
    "sample_dossier",
]
