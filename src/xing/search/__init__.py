"""检索层的入口。选后端只看配置，调用方拿到的是同一个 ``Searcher`` 协议。

三种后端的前提写在各自模块里，这里只强调一件事：**默认 stub 是给测试与
无 key 环境的，不是产品行为。** 它返回的每一条都标着 ``stub``，
``deepen`` 会把 ``brief.search.degraded`` 置为 True —— 让「这条内容建立在
假证据上」在数据库里看得见，而不是靠人记得改配置。
"""

from __future__ import annotations

from uuid import UUID

from xing.core.config import settings
from xing.search.base import (
    SearchConfigError,
    SearchDoc,
    Searcher,
    SearchError,
    SearchHit,
    SearchOptions,
    normalize_url,
    parse_date,
    squash,
)
from xing.search.qwen import QwenSearcher
from xing.search.stub import StubSearcher
from xing.search.tavily import TavilySearcher


def build_searcher(*, run_id: UUID | None = None, step_no: int = 0) -> Searcher:
    """按配置造一个检索器。

    ``run_id``/``step_no`` 只有模型端检索需要 —— 它的花费必须记在某个 run 上。
    HTTP 型检索拿不到它们也无所谓，传 None 即可。
    """
    provider = (settings.search_provider or "stub").strip().lower()

    if provider == "stub":
        return StubSearcher()

    if provider == "tavily":
        return TavilySearcher(
            api_key=settings.search_api_key,
            base_url=settings.search_base_url,
            timeout=settings.search_timeout,
        )

    if provider == "qwen":
        if settings.llm_provider != "openai":
            # 模型端检索要真的打到模型上。stub provider 会回一段伪文本，
            # 解析必然失败 —— 与其在运行到一半时报「结构不认识」，
            # 不如在构造时就说明是配置组合不成立。
            raise SearchConfigError(
                "search_provider=qwen 需要 llm_provider=openai（模型端检索走真实模型）"
            )
        if run_id is None:
            raise SearchConfigError("search_provider=qwen 需要 run_id（检索要记在 run 的账上）")
        return QwenSearcher(run_id=run_id, step_no=step_no)

    raise SearchConfigError(f"未知 search_provider: {provider!r}（可选 stub / tavily / qwen）")


__all__ = [
    "SearchConfigError",
    "SearchDoc",
    "SearchError",
    "SearchHit",
    "Searcher",
    "SearchOptions",
    "build_searcher",
    "normalize_url",
    "parse_date",
    "squash",
]
