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


PLAN_SHAPE: dict[str, Shape] = {
    "content_title": "str",
    "research_brief": "str",
    "write_brief": "str",
    "review_focus": "str",
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


#: 形状 ↔ 模型的对照表。测试逐对校验，缺一对就红。
CONTRACTS: tuple[tuple[type[BaseModel], dict[str, Shape]], ...] = (
    (Plan, PLAN_SHAPE),
    (ResearchResult, RESEARCH_SHAPE),
    (ReviewVerdict, REVIEW_SHAPE),
)


__all__ = [
    "CONTRACTS",
    "PLAN_SHAPE",
    "RESEARCH_SHAPE",
    "REVIEW_SHAPE",
    "Plan",
    "ResearchResult",
    "ReviewVerdict",
    "Shape",
]
