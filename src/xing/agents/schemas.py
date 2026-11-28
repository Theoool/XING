"""Agent 的输入契约：Pydantic 模型 + 供 stub provider 合成响应的形状描述。

为什么必须有这一层：Phase 2 的每个 Agent 都要判断模型输出是否**可用**，
而不是把一段自由文本硬塞进数据库。自由文本解析的错误会推迟到更贵的地方爆发
（写进内容库、发到平台、被人读到）。所以三个 Agent 三个模型，校验不过就
``FAILED(INVALID_OUTPUT)``，中间不允许「尽力解析」。

``*_SHAPE`` 是同一份契约的镜像，用途只有一个：``StubProvider`` 用它合成一个
能被 ``model_cls`` 接受的确定性响应，让整条链路在没有 API key 的环境里也能
跑通、也能回归。形状与模型可能漂移，所以 ``tests/test_agent_contracts.py``
逐对校验 ``model_cls.model_validate(fill(shape))``（``fill`` 在 provider.py，
它只服务 stub 与这个测试）—— 漂了就是测试红。

形状描述词的约定（只要 ``fill`` 认识就行，刻意保持极简）：

    "str" / "int" / "float" / "bool"   → 对应类型的最小占位值
    "enum:A|B"                         → 取第一个（Literal 的合法值）
    ["str"]                            → 单元素列表
    {"k": "str"}                       → 对象
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Shape = Any


class Plan(BaseModel):
    """一次 goal 展开成三步所需的全部内容。

    为什么不是「模型输出任意 DAG」：业务只有一条链路（调研 → 写作 → 审核），
    图的结构是代码常量。让模型决定结构只会换来两类新失败（漏步、环）和
    零收益 —— 它真正该决定的是每一步**做什么**。所以这里只要三段 brief。

    ``content_title`` 单独要一个字段，而不是让写作 Agent 从 brief 里截一段：
    brief 是**执行说明**（"面向 CTO 写一条 60 秒脚本，结构为…"），
    拿它当标题会写出「面向CTO及企业AI项目负责人，撰写60秒短视频脚本。结构采」
    这种东西 —— 真模型跑一遍就暴露了，截断位置在句号后面半句。
    """

    content_title: str = Field(min_length=1, max_length=120)
    research_brief: str = Field(min_length=1, max_length=2000)
    write_brief: str = Field(min_length=1, max_length=2000)
    review_focus: str = Field(min_length=1, max_length=1000)


class ResearchResult(BaseModel):
    """调研结论。只留可引用的事实摘要与关键词，不收原始网页。"""

    summary: str = Field(min_length=1, max_length=4000)
    keywords: list[str] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)


class ReviewVerdict(BaseModel):
    """审核意见。Agent 只给建议，通过与否由人决定（approvals）。"""

    verdict: Literal["PASS", "FAIL"]
    score: int = Field(ge=0, le=100)
    issues: list[str] = Field(default_factory=list)


class TopicCandidate(BaseModel):
    """一个候选选题。五个字段各回答一个不同的问题，缺任一个就没法打分或没法写：

    - ``title`` 这个选题叫什么（就是内容标题候选，不是执行说明）
    - ``angle`` 从哪切 —— 决定「和已有内容是否重复」，是差异化的唯一载体
    - ``audience`` 写给谁 —— ``relevance`` 的判据
    - ``thesis`` 核心论点 —— 写作时的骨架，写作 Agent 不该自己再想一遍
    - ``why_now`` 为什么现在做 —— ``potential`` 的判据

    刻意不要内容大纲：那是深入阶段的事。展开阶段就要大纲，模型会把六个候选
    写成六篇缩微文章，反而看不出角度差异 —— 而「多个候选之间真的不同」正是
    这一步唯一的价值。
    """

    title: str = Field(min_length=1, max_length=120)
    angle: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=100)
    thesis: str = Field(min_length=1, max_length=300)
    why_now: str = Field(min_length=1, max_length=200)


class TopicCandidates(BaseModel):
    """一次展开的全部候选。

    下限 3：少于 3 个就谈不上「挑」，也让评分失去区分度。
    上限 8：再多只会让每个都变薄 —— 模型在固定输出预算下摊薄注意力。
    """

    candidates: list[TopicCandidate] = Field(min_length=3, max_length=8)


class TopicScore(BaseModel):
    """单个候选的评分。``index`` 是 1-based 序号，不是 UUID。

    与 ``source_ref`` 同一个理由：模型编不出不存在的序号，代码映射回真实 id，
    比事后校验便宜。越界的 index 直接丢弃。
    """

    index: int = Field(ge=1, le=8)
    relevance: float = Field(ge=0, le=1)
    potential: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=400)


class TopicScores(BaseModel):
    scores: list[TopicScore] = Field(min_length=1, max_length=8)


class SearchQuery(BaseModel):
    """一条检索需求。``purpose`` 不是装饰，它决定这条 query 换回来的证据挂什么角色：
    找反面事实（COUNTER）和补数据（DATA）不能混成一句「查一下这个题」。
    """

    query: str = Field(min_length=2, max_length=200)
    purpose: Literal["FACT", "COUNTER", "DATA", "BACKGROUND"]


class SearchPlan(BaseModel):
    """深入阶段的第一件事：把「要查什么」写成可执行的 query。

    下限 2 是刻意的：只查一句，等于没查 —— 单一视角的检索必然只带回支持
    既有论点的材料。上限 5 是成本闸门，每多一条就是一次真实的检索调用。
    """

    queries: list[SearchQuery] = Field(min_length=2, max_length=5)


class OpinionQuery(BaseModel):
    """一条舆论检索需求。``target`` 决定这条 query 去找哪一类声音。

    与深入的 ``SearchQuery`` 分开，因为要的东西不同：深入要事实与数据，
    展开要**分歧本身**。只查「背景与现状」的地方，永远长不出好选题。
    """

    query: str = Field(min_length=2, max_length=200)
    target: Literal["DISPUTE", "OPPOSE", "SUPPORT", "VOICE", "SEGMENT"]


class OpinionPlan(BaseModel):
    """采信范围：同一条舆论，至少要有对立面、有亲历者、有受影响群体。"""

    queries: list[OpinionQuery] = Field(min_length=3, max_length=5)


class StanceItem(BaseModel):
    """从材料里抽出来的一条立场。

    ``quote`` 会在落库前做子串机检（见 ``agents.expander``）：
    模型可以概括 ``claim``，但 ``quote`` 必须是材料里真实存在的句子。
    ``audience`` 不是「大众」这种词，而是「谁在什么处境下会这么想」——
    它后面会成为候选的目标读者，写「大众」等于没有受众。
    """

    doc_index: int = Field(ge=1, le=32)
    label: str = Field(min_length=2, max_length=40)
    camp: Literal["SUPPORT", "OPPOSE", "CONDITIONAL", "EXPERIENCE", "OBSERVER"]
    claim: str = Field(min_length=2, max_length=240)
    audience: str = Field(min_length=2, max_length=120)
    quote: str = Field(min_length=2, max_length=400)


class StanceSet(BaseModel):
    """从一批材料里抽出的立场。**不设下限**，由调用方按轮次判定。

    下限 3 曾经写在这里，但它把「材料里确实只有一条声音」变成了
    ``INVALID_OUTPUT``（模型没错，是我们把采样结果当成了格式错误），
    真正的诊断 —— 立场不足、该不该补采 —— 反而被埋在 repair 日志里。
    现在空列表也合法：第一轮为空 → ``NO_STANCE_EXTRACTED``；不满 3 条或
    不满 2 个阵营 → ``OPINION_TOO_THIN``；补采轮为空 → 缺口照实记录。
    """

    stances: list[StanceItem] = Field(default_factory=list, max_length=16)


class GroundedCandidate(TopicCandidate):
    """候选必须声明它建立在哪些立场上（1-based 立场序号）。

    这是整条链上最关键的一处约束：``stance_indexes`` 为空或越界，
    候选就不会落库。模型想不出切面时，唯一的选择是从材料里找，
    而不是从记忆里编。
    """

    stance_indexes: list[int] = Field(min_length=1, max_length=5)


class GroundedCandidates(BaseModel):
    candidates: list[GroundedCandidate] = Field(min_length=3, max_length=8)


class OpinionVerdictItem(BaseModel):
    """对单个候选的舆论检测结论。只判「这个切面在不在舆论里」，
    不判真假 —— 真假是深入阶段的事。"""

    candidate_index: int = Field(ge=1, le=8)
    verdict: Literal["GROUNDED", "WEAK", "UNGROUNDED"]
    audience_identified: bool
    overlap_with: list[int] = Field(default_factory=list, max_length=4)
    reason: str = Field(min_length=1, max_length=300)


class OpinionVerdicts(BaseModel):
    verdicts: list[OpinionVerdictItem] = Field(min_length=1, max_length=8)
    missing_camps: list[str] = Field(default_factory=list, max_length=5)
    note: str = Field(default="", max_length=600)


class EvidenceItem(BaseModel):
    """一条证据：哪份材料（``doc_index``）、扮演什么角色、原文哪一句。

    ``quote`` 会在落库前做子串机检（见 ``agents.deepener``）——
    模型可以把 ``claim`` 写成自己的话，但 ``quote`` 必须是材料里真实存在的字符串。
    这是「不靠 LLM judge 也能挡住编造引文」的唯一办法。
    """

    doc_index: int = Field(ge=1, le=32)
    role: Literal["PRIMARY", "SUPPORTING", "CONTRADICTING", "BACKGROUND"]
    quote: str = Field(min_length=1, max_length=600)
    claim: str = Field(min_length=1, max_length=300)


class DeepenResult(BaseModel):
    """深入阶段的产出：一份能直接交给写作者的 brief，加上它的证据。

    和展开阶段的 ``TopicCandidate`` 是两种东西：候选只要「角度不同」，
    这里要「能写」—— 有论点、有结构、有反例、有出处。
    """

    thesis: str = Field(min_length=1, max_length=500)
    differentiator: str = Field(min_length=1, max_length=500)
    outline: list[str] = Field(min_length=3, max_length=8)
    counterpoints: list[str] = Field(default_factory=list, max_length=5)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=12)


PLAN_SHAPE: dict[str, Shape] = {
    "content_title": "str",
    "research_brief": "str",
    "write_brief": "str",
    "review_focus": "str",
}

SEARCH_PLAN_SHAPE: dict[str, Shape] = {
    "queries": [{"query": "str", "purpose": "enum:FACT|COUNTER|DATA|BACKGROUND"}]
}

DEEPEN_SHAPE: dict[str, Shape] = {
    "thesis": "str",
    "differentiator": "str",
    "outline": ["str"],
    "counterpoints": ["str"],
    "evidence": [
        {
            "doc_index": "int",
            "role": "enum:PRIMARY|SUPPORTING|CONTRADICTING|BACKGROUND",
            "quote": "str",
            "claim": "str",
        }
    ],
}

RESEARCH_SHAPE: dict[str, Shape] = {
    "summary": "str",
    "keywords": ["str"],
    "source_notes": ["str"],
}

REVIEW_SHAPE: dict[str, Shape] = {
    "verdict": "enum:PASS|FAIL",
    "score": "int",
    "issues": ["str"],
}

TOPIC_CANDIDATES_SHAPE: dict[str, Shape] = {
    "candidates": [
        {
            "title": "str",
            "angle": "str",
            "audience": "str",
            "thesis": "str",
            "why_now": "str",
        }
    ]
}

TOPIC_SCORES_SHAPE: dict[str, Shape] = {
    "scores": [
        {
            "index": "int",
            "relevance": "float",
            "potential": "float",
            "reason": "str",
        }
    ]
}

OPINION_PLAN_SHAPE: dict[str, Shape] = {
    "queries": [{"query": "str", "target": "enum:DISPUTE|OPPOSE|SUPPORT|VOICE|SEGMENT"}]
}

#: 与 ``OPINION_PLAN_SHAPE`` 结构相同，但只要求 1 条 —— 补采是窄查询。

STANCE_SET_SHAPE: dict[str, Shape] = {
    "stances": [
        {
            "doc_index": "int",
            "label": "str",
            "camp": "enum:SUPPORT|OPPOSE|CONDITIONAL|EXPERIENCE|OBSERVER",
            "claim": "str",
            "audience": "str",
            "quote": "str",
        }
    ]
}

GROUNDED_CANDIDATES_SHAPE: dict[str, Shape] = {
    "candidates": [
        {
            "title": "str",
            "angle": "str",
            "audience": "str",
            "thesis": "str",
            "why_now": "str",
            "stance_indexes": ["int"],
        }
    ]
}

OPINION_VERDICTS_SHAPE: dict[str, Shape] = {
    "verdicts": [
        {
            "candidate_index": "int",
            "verdict": "enum:GROUNDED|WEAK|UNGROUNDED",
            "audience_identified": "bool",
            "overlap_with": ["int"],
            "reason": "str",
        }
    ],
    "missing_camps": ["str"],
    "note": "str",
}


#: 形状 ↔ 模型的对照表。测试逐对校验，缺一对就红。
CONTRACTS: tuple[tuple[type[BaseModel], dict[str, Shape]], ...] = (
    (Plan, PLAN_SHAPE),
    (ResearchResult, RESEARCH_SHAPE),
    (ReviewVerdict, REVIEW_SHAPE),
    (TopicCandidates, TOPIC_CANDIDATES_SHAPE),
    (TopicScores, TOPIC_SCORES_SHAPE),
    (SearchPlan, SEARCH_PLAN_SHAPE),
    (DeepenResult, DEEPEN_SHAPE),
    (OpinionPlan, OPINION_PLAN_SHAPE),
    (StanceSet, STANCE_SET_SHAPE),
    (GroundedCandidates, GROUNDED_CANDIDATES_SHAPE),
    (OpinionVerdicts, OPINION_VERDICTS_SHAPE),
)


__all__ = [
    "CONTRACTS",
    "DEEPEN_SHAPE",
    "GROUNDED_CANDIDATES_SHAPE",
    "OPINION_PLAN_SHAPE",
    "OPINION_VERDICTS_SHAPE",
    "PLAN_SHAPE",
    "RESEARCH_SHAPE",
    "REVIEW_SHAPE",
    "SEARCH_PLAN_SHAPE",
    "STANCE_SET_SHAPE",
    "TOPIC_CANDIDATES_SHAPE",
    "TOPIC_SCORES_SHAPE",
    "DeepenResult",
    "EvidenceItem",
    "GroundedCandidate",
    "GroundedCandidates",
    "OpinionPlan",
    "OpinionQuery",
    "OpinionVerdictItem",
    "OpinionVerdicts",
    "Plan",
    "ResearchResult",
    "ReviewVerdict",
    "SearchPlan",
    "SearchQuery",
    "Shape",
    "StanceItem",
    "StanceSet",
    "TopicCandidate",
    "TopicCandidates",
    "TopicScore",
    "TopicScores",
]
