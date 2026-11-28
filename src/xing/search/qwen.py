"""模型端联网搜索（DashScope / 百炼 ``enable_search``）。

它和 tavily 的根本差别不是「谁更准」，而是**证据的形状**：这条接口不给
结构化的检索结果，只给一段检索完之后自己总结出来的答案。所以必须多用一层
约束把它逼回可校验的样子 —— 要求模型输出 ``{claim, quote, source}`` 三元组，
其中 ``quote`` 必须是它读到的原话。

这里有一个刻意的选择：**检索调用走 ``runtime.call_model``，不自己发 HTTP。**
因为模型联网是计费的，而一次检索动辄七千 input tokens，绕开 Runtime 就等于
绕开预算与账本 —— run 的 ``max_cost`` 会变成一个拦不住任何东西的数字。
代价是每次检索都要带 run_id/step_no，这是应该付的。

已知限制（写进 ``brief.search``，不藏着）：这类接口通常不返回可点击链接，
``url`` 常为空，证据只能回溯到「媒体名 + 日期」。要可点击的原文，用 tavily。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import UUID

import structlog

from xing.agents import runtime
from xing.core.config import settings
from xing.models.runtime import LlmPurpose
from xing.search.base import SearchDoc, SearchError, SearchHit, SearchOptions, parse_date

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """你是 XING 的检索 Agent。你会拿到一个检索需求，必须联网检索后回答。

只输出 JSON：
{"findings": [{"claim": "一句可被引用的事实判断",
               "quote": "检索到的原话，逐字摘录，不要改写、不要拼接",
               "source": "媒体名 + 日期；知道网址就把网址附在最后",
               "published_at": "YYYY-MM-DD，不知道就空串"}]}

硬性要求：
1. quote 必须是你真正读到的句子。拼不出来就给空数组，绝不编造。
2. 一条 finding 只讲一件事。宁可少给，不要为了凑数把同一件事拆成三条。
3. source 不要写「网络」「公开报道」这种没有指向的说法；不知道出处就不给这条。
4. 只输出 JSON，不要 Markdown 代码块、不要解释。"""


class QwenSearcher:
    name = "qwen"
    #: 模型端检索，每次调用都要在同一个 run 里占一个 call_seq。
    #: 并发插入会撞 ``llm_calls_call_seq`` 唯一索引，所以只能串行。
    concurrent_safe = False

    def __init__(self, *, run_id: UUID, step_no: int, max_output_tokens: int = 900) -> None:
        self._run_id = run_id
        self._step_no = step_no
        self._max_output_tokens = max_output_tokens

    async def search(
        self,
        query: str,
        *,
        max_results: int = 4,
        options: SearchOptions | None = None,
    ) -> SearchHit:
        """``options`` 在这个实现里不生效：检索是模型自己做的，接口不接受
        ``include_domains`` 之类的参数。原样回传，让轨迹如实显示「这层没用上」。
        """
        effective = options or SearchOptions()
        started = time.perf_counter()
        response = await runtime.call_model(
            self._run_id,
            purpose=LlmPurpose.TOOL_DECISION,
            step_no=self._step_no,
            system=SYSTEM_PROMPT,
            prompt=f"检索需求：{query}\n请给出最多 {max(1, min(max_results, 6))} 条。",
            max_output_tokens=self._max_output_tokens,
            web_search=True,
            # 检索正文由供应商注入，不在我们发的 prompt 里 —— 预留额度必须算上它。
            prompt_overhead=settings.search_context_chars,
        )

        docs = tuple(_to_docs(response.text, query, max_results))
        if not docs:
            raise SearchError(f"联网检索没有返回可用结果（{query[:40]}）")
        return SearchHit(
            query=query,
            docs=docs,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            options=effective,
        )


def _to_docs(text: str, query: str, limit: int) -> list[SearchDoc]:
    payload = _loads(text)
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        raise SearchError(f"检索返回结构不认识（{query[:40]}）")

    docs: list[SearchDoc] = []
    for item in findings[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or "").strip()
        quote = str(item.get("quote") or "").strip()
        source = str(item.get("source") or "").strip()
        if not quote or not source:
            # 没有原话或没有出处，就不是证据 —— 宁可少一条，也不留下一行
            # 谁也追溯不了的「事实」。
            continue
        url = _extract_url(source)
        docs.append(
            SearchDoc(
                title=(claim or quote[:80])[:200],
                snippet=quote,
                url=url,
                source_label=_strip_url(source) or None,
                published_at=parse_date(item.get("published_at")),
                raw={"provider": "qwen", "query": query, "source": source},
            )
        )
    return docs


def _loads(text: str) -> Any:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise SearchError("检索输出里没有 JSON 对象")
    try:
        return json.loads(body[start : end + 1])
    except ValueError as exc:
        raise SearchError(f"检索输出不是合法 JSON：{exc}") from exc


_URL = re.compile(r"https?://[^\s，。）)」\"']+")


def _extract_url(source: str) -> str | None:
    found = _URL.search(source)
    return found.group(0) if found else None


def _strip_url(source: str) -> str:
    return _URL.sub("", source).strip(" ，,。;；")


__all__ = ["QwenSearcher"]
