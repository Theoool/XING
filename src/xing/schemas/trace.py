"""取证轨迹的对外结构。

它不是「任务详情」的补充说明，而是回答一个任务**具体怎么做的**：

    规划（模型写了哪几条 query，每条想找什么声音）
      → 检索（真正下发了什么参数，回来几条，每条来自哪个域名、被归成哪类、
        为什么留下或丢掉）
      → 立场（从哪篇材料的哪句话里抽出来的）
      → 模型步骤（每一步的 system / prompt / 响应 / 耗时 / 花费）

四段各自独立，因为读的人在不同阶段关心不同的一段：调检索的只看第二段，
调 prompt 的只看第四段。塞成一坨就得靠猜。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class TraceDoc(BaseModel):
    """一条检索结果，以及它在这一轮里的命运。"""

    rank: int
    title: str
    url: str | None = None
    domain: str | None = None
    #: MEDIA / UGC / OFFICIAL / VESTED / UNKNOWN
    source_kind: str
    #: 命中的分类规则（``domain:zhihu.com`` / ``suffix:.gov.cn`` / ``unclassified``）。
    #: 判错的时候，先看的是这一条 —— 结果对不对取决于规则对不对。
    kind_rule: str
    published_at: datetime | None = None
    kept: bool
    #: EXCLUDED_PREVIOUS_ROUND / DUPLICATE / OVER_LIMIT / null
    drop_reason: str | None = None
    #: 按**今天**的分类表重算的结果，只在它与 ``source_kind`` 不一致时给出。
    #:
    #: 轨迹是那一趟的快照：``source_kind`` 必须是当时写下的判定，否则就是在
    #: 篡改记录。但分类表会变（补一个国别域名、加一条律所规则），于是旧轨迹
    #: 上会留着一批「未分类」，读的人以为分类器认不出来，于是去改本来已经
    #: 修好的那条规则。这里把两个答案都摆出来：当时判成什么、现在会判成什么。
    source_kind_now: str | None = None
    #: 现在会命中的那条规则。光有结论不够 —— 「会变成 VESTED」不告诉你
    #: 该改哪一行，``pattern:lawyer`` 才告诉你。
    kind_rule_now: str | None = None


class TraceQuery(BaseModel):
    seq: int
    #: MEDIA / DISCUSSION / SUPPLEMENT
    layer: str
    query: str
    target: str | None = None
    provider: str
    #: 真正下发的参数：topic / time_range / include_domains / exclude_domains。
    options: dict[str, Any]
    #: OK / EMPTY / ERROR
    status: str
    latency_ms: int
    result_count: int
    new_count: int
    error: str | None = None
    docs: list[TraceDoc]


class TraceRound(BaseModel):
    round: int
    queries: list[TraceQuery]
    query_count: int
    result_count: int
    new_count: int
    #: 这一轮留下的材料的来源构成。
    source_mix: dict[str, int]


class TraceStep(BaseModel):
    """一次模型调用。``prompt`` / ``system`` 是原文（超长会截断并标记）。"""

    step_no: int
    call_seq: int
    purpose: str
    status: str
    model: str
    latency_ms: int | None = None
    input_tokens: int
    output_tokens: int
    cost: float
    system: str | None = None
    prompt: str | None = None
    response: str | None = None
    error: str | None = None
    truncated: bool = False


class TraceStance(BaseModel):
    """一条立场与它的出处。这是「候选凭什么这么切」的最终依据。"""

    id: UUID
    label: str
    camp: str
    claim: str
    audience: str
    quote: str
    source_kind: str
    domain: str | None = None
    url: str | None = None
    title: str | None = None


class TraceSummary(BaseModel):
    query_count: int
    doc_count: int
    kept_count: int
    stance_count: int
    source_mix: dict[str, int]
    #: 0~1。0 表示这一轮的材料没有任何一条来自用户发言。
    ugc_ratio: float


class TaskTrace(BaseModel):
    task_id: UUID
    task_type: str
    status: str
    attempt: int
    dedup_key: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None
    #: 任务的输入/输出原样给出：调参时要看的就是「当时传了什么」。
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    run_id: UUID | None = None
    agent: str | None = None
    goal: str | None = None
    run_status: str | None = None
    rounds: list[TraceRound]
    steps: list[TraceStep]
    stances: list[TraceStance]
    summary: TraceSummary


__all__ = [
    "TaskTrace",
    "TraceDoc",
    "TraceQuery",
    "TraceRound",
    "TraceStance",
    "TraceStep",
    "TraceSummary",
]
