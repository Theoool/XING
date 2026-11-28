"""检索的契约：一次 query → 若干条可落库的文档。

为什么是「文档」而不是「一段回答」：这条链的下游要的是**证据**——能写进
``external_events``、能被 ``topic_events`` 挂到选题上、能被审核 Agent 拿去
比对正文有没有夸大。一段自由文本做不到这三件事，所以 ``SearchDoc`` 的字段
直接对齐 ``external_events`` 的列（title / snippet / url / published_at）。

两类后端的前提不同，字段也据此分叉：

- **显式搜索 API**（tavily）：给的是网页标题、链接、正文片段。``url`` 一定有，
  证据能一路回溯到原文。
- **模型端联网搜索**（qwen）：模型检索完自己总结，给的是「结论 + 原话 + 出处
  （媒体名/日期）」。``url`` 通常为空 —— 这不是缺陷，是那类接口的事实：
  它不把检索结果的结构化引用返回给你。所以 ``source_label`` 必须填，
  否则证据连「哪家媒体」都说不出来。

``identity`` 是落库的去重键。同一个 URL 被五条 query 命中只该有一行；
没有 URL 时退化为「出处 + 标题 + 片段」的哈希 —— 不同 query 命中同一条
报道时会落到同一个键上，正是想要的。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


#: 检索失败的统一异常。Agent 只在「展开」里容忍它，在「深入」里不容忍 ——
#: 展开可以纯推理，深入就是来拿事实的，搜不到就不该假装写完了。
class SearchError(RuntimeError):
    """检索不可用：配额、超时、返回结构不认识、结果为空。"""


class SearchConfigError(SearchError):
    """配置不对（缺 key、provider 名字写错、组合不成立）。

    单独一个类型，是因为它和「这次没搜到」的处理方式相反：配置错必须让链路
    立刻停（否则整条管线在静默地产生没有事实依据的内容），而单次检索失败
    在展开阶段是可以容忍的。
    """


@dataclass(frozen=True, slots=True)
class SearchOptions:
    """一次检索的附加参数。

    和检索结果分开建模，是为了让「发出去的参数」和「给人看的参数」是同一个
    对象：轨迹里存的就是这个 dataclass 的 ``as_dict()``，不存在两份口径。
    """

    #: tavily 的 ``topic``：``general`` / ``news``。留空由 provider 决定。
    #:
    #: 放开是有意的：``topic=news`` 会隐式带一个很短的时间窗（默认 7 天），
    #: 对「远程办公三年后」这类旧题会把材料搜空。要不要收窄，交给调用方。
    topic: str | None = None
    #: ``day`` / ``week`` / ``month`` / ``year``。留空 = 不限时间。
    time_range: str | None = None
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "time_range": self.time_range,
            "include_domains": list(self.include_domains),
            "exclude_domains": list(self.exclude_domains),
        }


@dataclass(frozen=True, slots=True)
class SearchDoc:
    title: str
    snippet: str
    url: str | None = None
    #: 没有 URL 时的出处标签（媒体名 + 日期）。有 URL 也要填，便于人读。
    source_label: str | None = None
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        """``external_events.external_id`` 的取值。"""
        basis = self.url or f"{self.source_label or ''}|{self.title}|{self.snippet}"
        return hashlib.sha256(_squash(basis).encode("utf-8")).hexdigest()[:40]

    @property
    def source(self) -> str:
        """给人看的来源：有链接给链接，没有给出处标签，都没有就说明没有。"""
        return self.url or self.source_label or "来源未注明"


@dataclass(frozen=True, slots=True)
class SearchHit:
    query: str
    docs: tuple[SearchDoc, ...]
    provider: str
    latency_ms: int = 0
    #: 实际生效的参数（provider 原样回传）。轨迹以它为准，不以调用方以为的为准。
    options: SearchOptions = field(default_factory=SearchOptions)


class Searcher(Protocol):
    name: str

    #: 同一个 run 内能否并发调用。
    #:
    #: 由实现方自己声明，因为限制来自记账方式而不是网络：模型端检索走
    #: ``runtime.call_model``，``call_seq`` 是 ``max+1``，两条并发会撞
    #: ``llm_calls_call_seq`` 唯一索引；HTTP 型检索不写 ``llm_calls``，
    #: 并发是安全的。展开一次 3~5 条 query，串行等待是它最大的空转。
    concurrent_safe: bool

    async def search(
        self,
        query: str,
        *,
        max_results: int = 4,
        options: SearchOptions | None = None,
    ) -> SearchHit: ...


_WS = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def squash(text: str) -> str:
    """折叠空白，供「引文必须是原文子串」这条机检使用。

    不能直接 ``in``：模型会把换行写成空格、把两个空格并成一个，
    这样判出来的「不合法」是格式差异，不是编造。
    """
    return _WS.sub(" ", text).strip()


def normalize_url(url: str | None) -> str | None:
    """去掉片段标识与末尾斜杠。``#section`` 不改变文档身份。"""
    if not url:
        return None
    cleaned = url.strip().split("#", 1)[0].rstrip("/")
    return cleaned or None


def parse_date(raw: Any) -> datetime | None:
    """``published_at`` 的宽容解析。只认 ISO 日期/日期时间，其余当没有。

    编一个「今天」比留空更糟：趋势分与时效判断都会当真。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None


__all__ = [
    "SearchConfigError",
    "SearchDoc",
    "SearchError",
    "SearchHit",
    "SearchOptions",
    "Searcher",
    "normalize_url",
    "parse_date",
    "squash",
]
