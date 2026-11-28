"""确定性检索：同样的 query 永远返回同样的文档，不联网。

它存在的理由和 ``StubProvider`` 完全一样 —— 没有搜索 key 的环境里也要能把
「检索 → 证据 → brief → 写作」这条链跑通并回归。但它**不产生真实信息**，
所以每一行的 ``source_label`` 都写着 ``stub``，``brief.search.degraded``
会被置为 True：让「这次深入用的是假数据」在库里是可见的，而不是一个
长得像真的摘要。
"""

from __future__ import annotations

import hashlib
import time

from xing.search.base import SearchDoc, SearchHit, SearchOptions


class StubSearcher:
    name = "stub"
    #: 不碰网络也不记账，怎么并发都一样。
    concurrent_safe = True

    async def search(
        self,
        query: str,
        *,
        max_results: int = 4,
        options: SearchOptions | None = None,
    ) -> SearchHit:
        started = time.perf_counter()
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        count = max(1, min(max_results, 4))

        docs = tuple(
            SearchDoc(
                title=f"stub 文档 {index}：{query}"[:200],
                # 片段里必须出现 "stub"：证据抽取的机检要求 quote 是 snippet 的
                # 子串，而 stub provider 合成的 quote 恒为 "stub"。
                snippet=(
                    f"stub 检索结果 {index}：关于「{query}」的示例材料。"
                    "这里本来应该是一段来自公开报道的原文，包含具体数字、时间与出处；"
                    "当前使用 stub 检索器，所以这段文字没有任何事实效力。"
                ),
                url=f"https://stub.invalid/{digest[:12]}/{index}",
                source_label="stub",
                published_at=None,
                raw={"provider": "stub", "query": query, "index": index},
            )
            for index in range(1, count + 1)
        )
        return SearchHit(
            query=query,
            docs=docs,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            options=options or SearchOptions(),
        )


__all__ = ["StubSearcher"]
