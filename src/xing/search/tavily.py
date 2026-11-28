"""Tavily 搜索。《https://docs.tavily.com》—— 一次 POST 换回标题/链接/正文片段。

选它而不是自己爬：这条链需要的是「可引用的原文片段」，不是「一个看起来像
答案的段落」。Tavily 的 ``content`` 就是原文摘要，落进
``external_events.summary`` 后，证据引文可以做子串机检、链接可以直接点开。

失败一律抛 :class:`SearchError`，绝不在异常里返回空列表 ——
「搜不到」和「搜坏了」在链路下游的处理完全不同：前者是内容问题，
后者是基础设施问题，混在一起就再也分不清该重试还是该换题。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from xing.search.base import (
    SearchDoc,
    SearchError,
    SearchHit,
    SearchOptions,
    normalize_url,
    parse_date,
)


class TavilySearcher:
    name = "tavily"
    #: 纯 HTTP，不写 llm_calls，同一 run 里可以并发。
    concurrent_safe = True

    def __init__(self, *, api_key: str, base_url: str, timeout: float = 25.0) -> None:
        if not api_key:
            raise SearchError("search_provider=tavily 但没有 search_api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def search(
        self,
        query: str,
        *,
        max_results: int = 4,
        options: SearchOptions | None = None,
    ) -> SearchHit:
        started = time.perf_counter()
        effective = options or SearchOptions()
        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max(1, min(max_results, 10)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        # 只下发有值的参数。传 None 和「不传」在 provider 那边的语义不同，
        # 而轨迹要记录的是真正生效的那一组，所以这里逐项判断再写。
        if effective.topic:
            payload["topic"] = effective.topic
        if effective.time_range:
            payload["time_range"] = effective.time_range
        if effective.include_domains:
            payload["include_domains"] = list(effective.include_domains)
        if effective.exclude_domains:
            payload["exclude_domains"] = list(effective.exclude_domains)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._base_url}/search", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # 网络层错误。带上 query 便于事后判断是「这个词搜不到」还是「整站不可用」。
            raise SearchError(f"检索请求失败（{query[:40]}）：{exc}") from exc

        if resp.status_code >= 400:
            raise SearchError(f"检索返回 {resp.status_code}（{query[:40]}）：{resp.text[:300]}")

        try:
            body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise SearchError(f"检索返回非 JSON（{query[:40]}）") from exc

        docs = tuple(
            doc for item in body.get("results") or [] if (doc := _to_doc(item, query)) is not None
        )
        return SearchHit(
            query=query,
            docs=docs,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            options=effective,
        )


def _to_doc(item: Any, query: str) -> SearchDoc | None:
    if not isinstance(item, dict):
        return None
    url = normalize_url(str(item.get("url") or ""))
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("content") or item.get("snippet") or "").strip()
    if not snippet and not title:
        return None
    return SearchDoc(
        title=(title or snippet[:80])[:200],
        snippet=snippet or title,
        url=url,
        source_label=_label(url),
        published_at=parse_date(item.get("published_date")),
        raw={"provider": "tavily", "query": query, "score": item.get("score")},
    )


def _label(url: str | None) -> str | None:
    """从 URL 里取个可读出处。取不到就返回 None，不编媒体名。"""
    if not url:
        return None
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return host.removeprefix("www.") or None


__all__ = ["TavilySearcher"]
