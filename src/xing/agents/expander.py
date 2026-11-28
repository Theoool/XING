"""展开选题：一个种子 → 一组真实存在的舆论立场 → 多个带出处的候选。

这一环原来靠模型先验：给一段种子，让它想六个角度。问题不是「想得不好」，
而是**无法证明这六个角度和现实里的争论有关系**。同一个事件，不同人群在吵
什么、谁觉得自己被误伤、哪一方有条件的让步 —— 这些都在公开语料里，只是
从来没被采集过。

现在这条链的因果是倒过来的：

    1. 定舆论检索计划（争议 / 反方 / 支持 / 亲历者 / 受影响群体）
    2. 真的去搜，材料落 external_events(SEARCH)
    3. 从材料里抽立场（``opinion_stances``），quote 必须逐字命中材料
    4. 缺了哪个阵营就再补采一轮（VOICE / CONDITIONAL 优先），补不到如实留痕
    5. 只把**立场池**交给模型生成候选，每个候选必须引用立场序号
    6. 引用不到立场、或立场不足，展开直接失败 —— 不回退到模型记忆

第 3 步的机检是这条链的硬边界：``quote`` 不是材料子串就丢掉。它拦的不是
「模型文笔不好」，而是「这个立场其实没人说过」。

「切面是否真的存在于舆论」「目标群体是否可识别」「候选之间是不是换皮」由
独立的 ``check_opinion`` 判定（独立 run，避免同一上下文自评虚高）。**它不判
真假** —— 真假是深入阶段的事，这一环只负责「这个题有没有人真的在讨论」。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xing.agents import runtime, schemas
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.event import ExternalEvent, SourceType
from xing.models.runtime import LlmPurpose
from xing.models.strategy import Strategy, StrategyStatus, StrategyVersion
from xing.models.topic import OpinionStance, Topic, TopicStance, TopicStatus
from xing.models.trace import RetrievalTrace
from xing.schemas.task import TaskCreate
from xing.search import (
    SearchConfigError,
    SearchDoc,
    Searcher,
    SearchError,
    SearchHit,
    SearchOptions,
    build_searcher,
    squash,
)
from xing.search.sources import DISCUSSION_DOMAINS, SOURCE_KINDS, classify, domain_of
from xing.services.task_service import create_task
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "expander"

#: 默认展开数量。给 3 个谈不上挑，给 10 个会把每个都摊薄。
DEFAULT_COUNT = 6
MIN_COUNT = 3
MAX_COUNT = 8

MAX_OUTPUT_TOKENS_QUERIES = 600
MAX_OUTPUT_TOKENS_STANCES = 2200
MAX_OUTPUT_TOKENS_CANDIDATES = 2400

#: 一题最多收集多少篇材料。每条 query 一次检索，18 条是成本与覆盖的折中。
MAX_DOCS = 18
#: 进立场抽取 prompt 的材料条数。
DOCS_IN_PROMPT = 14
MAX_CHARS_PER_DOC = 500
#: 每条 query 取几条。展开要的是「不同声音」，不是「同一件事的十篇转载」。
SEARCH_PER_QUERY = 6

#: 立场池的下限。少于 3 个不同立场或只有 1 个阵营，说明这一题没有真实分歧。
MIN_STANCES = 3
MIN_CAMPS = 2

#: 舆论的五个阵营。补采按这个全集算缺口 —— 顺序即 prompt 里的枚举顺序。
ALL_CAMPS = ("SUPPORT", "OPPOSE", "CONDITIONAL", "EXPERIENCE", "OBSERVER")

#: 补采只针对「构成一边声音」的四个阵营。
#:
#: OBSERVER（旁观分析）不补：它不是分歧的一方，缺了不影响「两边都有人说话」。
#: 真实跑里为它多花过一轮（2 条 query / 8 篇材料），补回来的立场是
#: 「智能化因数字化未完成而受限」—— 讲的是实体经济，与周报毫无关系。
#: 缺口照实记录在 coverage 里，但不为它花检索预算。
SUPPLEMENT_CAMPS = ("SUPPORT", "OPPOSE", "CONDITIONAL", "EXPERIENCE")

#: 补采一轮最多收几篇。补采的目标很窄，收太多会把第一轮的题重新搜散。
MAX_SUPPLEMENT_DOCS = 8

#: 讨论层：只在「发言单位是人」的站点里检索。
#:
#: 媒体层回答「这件事被怎么报道」，讨论层回答「人自己怎么说」。真实数据里
#: 前者占 17/19 个域名 —— 少了这一层，所谓舆论就是媒体摘要。query 用模板拼，
#: 不额外调模型：这一层的意图是固定的（怎么看 / 亲历），只有词随题变。
DISCUSSION_QUERIES = ("{core} 怎么看", "{core} 亲历 真实感受")

#: 讨论层的时间窗。UGC 的时效性比媒体强：一条三年前的微博代表不了今天的舆论，
#: 而三年前的分析文章仍然可以说明争论的来路 —— 所以只有这一层收窄。
DISCUSSION_TIME_RANGE = "year"

SYSTEM_QUERIES = """你是 XING 的舆论检索规划 Agent。给定一个事件或方向，写出用来
采集**公众讨论**的检索需求。你要找的不是事实与背景，而是分歧本身。

硬性要求：
1. 3~5 条 query，每条只查一类声音。
2. 必须包含：至少一条 target=OPPOSE（反对意见、失败案例、质疑），
   至少一条 target=VOICE（亲历者、从业者、当事人的第一人称说法），
   至少一条 target=SEGMENT（某个具体群体的处境，如「中小厂法务」「一线审批员」）。
3. query 要像人在搜索框里输入的话，带主体与场景/时间限定；
   不要用「探讨」「分析」「现状与趋势」这类没有检索价值的词。
4. target 取值：
   DISPUTE   争议焦点（两边在吵什么）
   OPPOSE    反方与失败
   SUPPORT   支持方与成功案例
   VOICE     亲历者/从业者的说法
   SEGMENT   特定受影响群体的处境

只输出 JSON：{"queries": [{"query": "...", "target": "OPPOSE"}]}
不要输出解释、不要 Markdown 代码块。"""

#: 补采 query 模板。补采的目标很窄 —— 已知缺哪个阵营，只需要换一组词再搜一次。
#: 为此再调一次模型规划不划算：一次 plan 实测 4~5 秒，而模板可复现、可测试。
#: 每阵营最多两条，总数由 ``SUPPLEMENT_MAX_QUERIES`` 封顶（检索是并发的，但
#: 材料上限只有 8 篇，问太多等于把每一条都摊薄）。
_SUPPLEMENT_QUERIES: dict[str, tuple[str, ...]] = {
    "SUPPORT": ("{core} 确实有用 效果", "{core} 上线之后 团队"),
    "OPPOSE": ("{core} 问题 反对 踩坑",),
    "CONDITIONAL": ("{core} 前提 有条件 支持但是", "{core} 只要 就愿意"),
    "EXPERIENCE": ("{core} 亲历 体验 我", "{core} 三个月 实际"),
}

SUPPLEMENT_MAX_QUERIES = 3

SYSTEM_STANCES = """你是 XING 的舆论分析 Agent。你会看到一批带编号的检索材料。
任务：抽出材料里**明确表达出来的立场**。

硬性要求：
1. 只抽材料里真实写着的观点。不推理、不补充、不替材料说话。
2. quote 必须**逐字复制**材料里的原句（连续的一段），不能改写、不能拼接、
   不能翻译、不能只引几个词。
3. label 是这个立场的一句话标签（≤ 20 字），要能和别的立场区分开。
4. camp 取值：SUPPORT（支持）/ OPPOSE（反对）/ CONDITIONAL（有条件同意）/
   EXPERIENCE（亲历者第一人称）/ OBSERVER（旁观分析）。
5. audience 写「谁在什么处境下会这么想」，具体到人群与场景；
   禁止「大众」「网友」「所有人」。
6. 同一篇材料可以抽出多个立场（不同人说的不同话），但不要把一个意思拆成三条。
7. 材料里没有明确立场就给空数组，绝不编。至少给出 3 条，能覆盖两种以上 camp。

只输出 JSON：
{"stances": [{"doc_index": 1, "label": "...", "camp": "OPPOSE",
              "claim": "...", "audience": "...", "quote": "..."}]}
不要输出解释、不要 Markdown 代码块。"""

SYSTEM_STANCES_SUPPLEMENT = """你是 XING 的舆论分析 Agent。这是**补采**的一批材料：
它们是为了补上缺失的某一种声音才去搜的。

硬性要求：
1. 只抽材料里真实写着的观点。不推理、不补充、不替材料说话。
2. quote 必须**逐字复制**材料里的原句（连续的一段），不能改写、不能拼接。
3. 这批材料可能只有一两条能用的立场，甚至一条都没有 —— **这很正常**。
   有多少给多少，绝不为凑数编一条，也绝不把别的阵营硬说成缺失的那一种。
4. label ≤ 20 字；camp 取 SUPPORT / OPPOSE / CONDITIONAL / EXPERIENCE / OBSERVER；
   audience 具体到人群与场景，禁止「大众」「网友」。
5. 如果材料讲的事情与题目无关，就不要从里面抽立场。

只输出 JSON：
{"stances": [{"doc_index": 1, "label": "...", "camp": "CONDITIONAL",
              "claim": "...", "audience": "...", "quote": "..."}]}
没有就给 {"stances": []}。不要输出解释、不要 Markdown 代码块。"""

SYSTEM_CANDIDATES = """你是 XING 的选题 Agent。你会看到一个**立场池**（带编号），
以及一个事件/方向。请基于这些立场生成候选选题。

硬性要求：
1. 每个候选必须写 stance_indexes（至少 1 个），指向它建立在哪些立场上。
   这些立场必须是它真正回应的分歧，不是随便挂一个。
2. 不同候选必须落在**不同的舆论切面**，并且面向**不同的目标人群**。
   禁止同一立场的换皮：如果两个候选引用同一组立场、写给同一群人，它们就是一条。
3. angle 要写清「站在哪一方的判断上、从哪个分歧切进去」。
4. thesis 必须是一句可以被反驳的判断，不要复述标题。
5. audience 具体到人群与处境（如「要给 AI 审批签字的合规负责人」），
   不要写「大众」「企业」。
6. 只能使用立场池里出现过的信息。立场池里没有的数字、机构、时间一律不写。
   why_now 只能基于立场池里的时间线索；没有就写「目前仍在争论」。
7. 不要提出「是什么/为什么重要/怎么做」三段式换皮。

只输出 JSON：
{"candidates": [{"title": "...", "angle": "...", "audience": "...",
                 "thesis": "...", "why_now": "...", "stance_indexes": [1, 3]}]}
不要输出解释、不要 Markdown 代码块。"""


@dataclass(frozen=True, slots=True)
class _Doc:
    """一篇落库的材料 + 它在 prompt 里的编号。"""

    index: int
    doc: SearchDoc
    event_id: UUID
    #: 来源类型（MEDIA / UGC / ...）。跟着材料走，覆盖率统计直接读它。
    source_kind: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class _Retrieval:
    """一条**真正要执行**的检索：词 + 查哪一层 + 下发的参数。

    和执行结果分开，是因为这三样都要进轨迹。只记 query 文本的话，
    「为什么这一轮一条 UGC 都没有」就没法回答 —— 可能是没查那一层，
    也可能是查了但被域名限制卡死了。
    """

    text: str
    #: MEDIA（媒体层）/ DISCUSSION（讨论层）/ SUPPLEMENT（补采）
    layer: str
    target: str | None = None
    options: SearchOptions = field(default_factory=SearchOptions)
    limit: int = SEARCH_PER_QUERY


@dataclass(frozen=True, slots=True)
class _RoundTrace:
    """一条检索的取证记录。一列对一列地落 ``retrieval_traces``。"""

    seq: int
    retrieval: _Retrieval
    provider: str
    status: str = "OK"
    latency_ms: int = 0
    result_count: int = 0
    new_count: int = 0
    options: dict[str, Any] = field(default_factory=dict)
    docs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Stance:
    """一条通过机检的立场。``event_id`` 是它的出处。"""

    event_id: UUID
    label: str
    camp: str
    claim: str
    audience: str
    quote: str


@dataclass(frozen=True, slots=True)
class _Grounded:
    """一个引用了真实立场的候选。``indexes`` 已去重、已校验。"""

    candidate: schemas.GroundedCandidate
    indexes: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _Supplement:
    """补采一轮的结果。``queries`` 为空表示这一轮没发生。"""

    queries: tuple[str, ...] = field(default_factory=tuple)
    docs: int = 0
    #: 补回来的材料按来源类型的分布，和首轮合并后才能代表整个材料池。
    source_mix: dict[str, int] = field(default_factory=dict)
    stances: tuple[Stance, ...] = field(default_factory=tuple)
    dropped_stances: int = 0
    duplicate_stances: int = 0
    missing_before: tuple[str, ...] = field(default_factory=tuple)
    missing_after: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ran(self) -> bool:
        return bool(self.queries)


@handler("expand_topics")
async def expand_topics(task: TaskContext) -> dict[str, Any]:
    seed_type = str(task.input.get("seed_type") or "event").strip().lower()
    count = _clamp(int(task.input.get("count") or DEFAULT_COUNT))

    seed_text, seed_event_id = await _resolve_seed(task, seed_type)

    # 重跑幂等：这个任务已经展开过就不再调模型、不再花钱。
    reused = await _existing_topics(task)
    if reused:
        log.info("agent.expand_reused", task_id=str(task.id), count=len(reused))
        return {"topic_ids": [str(t) for t in reused], "count": len(reused), "reused": True}

    strategy_config = await _active_strategy_config(task.workspace_id)

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=seed_text[:2000],
            task_id=task.id,
            attempt=task.attempt,
            max_cost=_max_cost(task),
            # 五步：检索计划 / 立场抽取 / 候选生成，加上补采的计划与抽取；
            # 每步各留一次 repair 的余量。
            max_steps=9,
        )
    )

    try:
        # ---- 1. 舆论检索计划 ------------------------------------------
        plan_step = await runtime.begin_step(run_id)
        plan = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.PLAN,
            step_no=plan_step,
            system=SYSTEM_QUERIES,
            prompt=_plan_prompt(seed_text),
            schema=schemas.OpinionPlan,
            shape=schemas.OPINION_PLAN_SHAPE,
            max_output_tokens=MAX_OUTPUT_TOKENS_QUERIES,
        )

        try:
            searcher = build_searcher(run_id=run_id, step_no=plan_step)
        except SearchConfigError as exc:
            # 配置错重试不会变对。终态，而不是烧三次预算。
            raise NonRetryableError(str(exc), code="SEARCH_NOT_CONFIGURED") from exc

        # ---- 2. 真的去搜（媒体层 + 讨论层） ---------------------------
        retrievals = _plan_retrievals(plan.queries, _seed_core(seed_text))
        docs, traces = await _search_all(searcher, retrievals)
        # 先落轨迹再抽立场：这一步之后可能失败，而失败时最需要的正是「搜了什么」。
        await _record_traces(task, run_id, round_no=0, traces=traces)
        if not docs:
            # 没有材料就没有舆论，也就没有切面。这里不能退化成模型先验 ——
            # 那正是这次改造要消灭的东西。
            raise NonRetryableError(
                "检索没有返回任何舆论材料，无法建立立场池", code="NO_OPINION_SOURCE"
            )
        stored = await _persist_docs(task, docs)

        # ---- 3. 抽立场（quote 机检） -----------------------------------
        stance_step = await runtime.begin_step(run_id)
        raw = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.ANALYZE,
            step_no=stance_step,
            system=SYSTEM_STANCES,
            prompt=_stance_prompt(seed_text, stored),
            schema=schemas.StanceSet,
            shape=schemas.STANCE_SET_SHAPE,
            max_output_tokens=MAX_OUTPUT_TOKENS_STANCES,
        )
        stances, dropped_stances = _validate_stances(raw, stored)
        if not stances:
            raise NonRetryableError(
                "立场抽取没有一条引文通过机检（模型没有照抄材料）",
                code="NO_STANCE_EXTRACTED",
            )

        # ---- 4. 补采缺失阵营（最多一轮） -------------------------------
        # 缺口本身不阻断选题，但必须先试着补：缺的往往正是「另一边的声音」，
        # 少了它，六个候选会全站在同一侧。补不回来就如实记录在案 —— 不编。
        # 只补四个实义阵营（见 SUPPLEMENT_CAMPS），OBSERVER 缺了不补。
        missing_before = _missing_camps(stances, for_supplement=True)
        supplement = _Supplement(
            missing_before=tuple(missing_before), missing_after=tuple(missing_before)
        )
        if searcher.name != "stub" and missing_before:
            supplement = await _supplement(
                task,
                run_id,
                searcher,
                seed_text,
                stances,
                missing_before,
                known={item.doc.identity for item in stored},
            )
            stances = [*stances, *supplement.stances]
            dropped_stances += supplement.dropped_stances

        labels = {item.label for item in stances}
        camps = {item.camp for item in stances}
        supplement = replace(
            supplement, missing_after=tuple(_missing_camps(stances, for_supplement=True))
        )

        # stub 检索器只有一种材料、一种声音，多样性闸门在离线环境跳过；
        # 真实检索下必须过 —— 这正是「有没有人在吵」的判据。补采之后仍然
        # 过不去，说明这一题在公开语料里确实没有分歧，不该硬出题。
        if searcher.name != "stub" and (len(labels) < MIN_STANCES or len(camps) < MIN_CAMPS):
            raise NonRetryableError(
                f"舆论样本不足以支撑选题：只有 {len(labels)} 个立场 / {len(camps)} 个阵营"
                + (
                    f"（补采一轮后仍缺 {'、'.join(supplement.missing_after)}）"
                    if supplement.ran and supplement.missing_after
                    else ""
                ),
                code="OPINION_TOO_THIN",
            )

        stance_ids = await _persist_stances(task, stances)

        # ---- 5. 只把立场池交给模型，生成带出处的候选 --------------------
        gen_step = await runtime.begin_step(run_id)
        result = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.GENERATE,
            step_no=gen_step,
            system=SYSTEM_CANDIDATES,
            prompt=_candidate_prompt(seed_text, count, stances, strategy_config),
            schema=schemas.GroundedCandidates,
            shape=schemas.GROUNDED_CANDIDATES_SHAPE,
            max_output_tokens=MAX_OUTPUT_TOKENS_CANDIDATES,
        )
        grounded, dropped_candidates = _ground(result, stances)
        if not grounded:
            raise NonRetryableError(
                "没有任何候选引用到有效的舆论立场", code="NO_GROUNDED_CANDIDATE"
            )

        topic_ids = await _persist(
            task,
            grounded,
            stances,
            stance_ids,
            seed_event_id,
            run_id,
            provider=searcher.name,
            doc_count=len(stored) + supplement.docs,
            source_mix=_merge_mix(_source_mix(stored), supplement.source_mix),
            supplement=supplement,
        )

        await runtime.finish_run(
            run_id,
            output={
                "topic_ids": [str(item) for item in topic_ids],
                "stance_count": len(stances),
                "doc_count": len(stored) + supplement.docs,
                "provider": searcher.name,
                "source_mix": _merge_mix(_source_mix(stored), supplement.source_mix),
                "missing_camps": list(supplement.missing_after),
                "supplement": _supplement_output(supplement),
            },
        )
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    # 交给独立的舆论检测。它是闸门，不是建议 —— 不合格的候选会在那里被拒。
    opinion_task_id = await _enqueue_opinion_check(task)

    log.info(
        "agent.expanded",
        task_id=str(task.id),
        count=len(topic_ids),
        seed_type=seed_type,
        stances=len(stances),
        docs=len(stored) + supplement.docs,
        dropped_stances=dropped_stances,
        dropped_candidates=dropped_candidates,
        supplement_rounds=1 if supplement.ran else 0,
        missing_camps=list(supplement.missing_after),
        provider=searcher.name,
        opinion_task=str(opinion_task_id) if opinion_task_id else None,
    )
    return {
        "topic_ids": [str(t) for t in topic_ids],
        "count": len(topic_ids),
        "run_id": str(run_id),
        "stance_count": len(stances),
        "doc_count": len(stored) + supplement.docs,
        "dropped_stances": dropped_stances,
        "dropped_candidates": dropped_candidates,
        "provider": searcher.name,
        # 缺口与补采留痕：界面/脚本据此判断「这一题是不是只有一边的声音」。
        "missing_camps": list(supplement.missing_after),
        "supplement": _supplement_output(supplement),
        "opinion_task_id": str(opinion_task_id) if opinion_task_id else None,
        # 兼容旧输出字段：展开现在必然有外部材料，否则根本不会走到这里。
        "seed_docs": len(stored) + supplement.docs,
    }


def _supplement_output(supplement: _Supplement) -> dict[str, Any]:
    """补采的可序列化留痕。没发生补采时也给出 missing_after。"""
    return {
        "rounds": 1 if supplement.ran else 0,
        "queries": list(supplement.queries),
        "new_docs": supplement.docs,
        "new_stances": len(supplement.stances),
        "duplicate_stances": supplement.duplicate_stances,
        "dropped_stances": supplement.dropped_stances,
        "missing_before": list(supplement.missing_before),
        "missing_after": list(supplement.missing_after),
    }


# ------------------------------------------------------------------ 检索


async def _search_one(searcher: Searcher, retrieval: _Retrieval) -> SearchHit | SearchError:
    """单条检索，失败不抛而是把异常当返回值。

    并发时要的是「其他几条照常完成」，所以异常必须在协程内部收住；
    串行路径复用同一个函数，两条路径的容错口径就不会分叉。
    """
    try:
        return await searcher.search(
            retrieval.text,
            max_results=retrieval.limit,
            options=retrieval.options,
        )
    except SearchError as exc:
        log.warning(
            "agent.opinion_search_failed",
            query=retrieval.text,
            layer=retrieval.layer,
            error=str(exc)[:200],
        )
        return exc


def _plan_retrievals(items: list[schemas.OpinionQuery], core: str) -> list[_Retrieval]:
    """媒体层照模型给的查，讨论层用模板补两条。

    为什么要分层：模型规划的 query 没有「查哪个站」的概念，落到通用检索上就
    只会命中媒体与专栏。而「舆论」的定义里必须有一部分是普通人自己在说话。
    这一层只加两条、只在固定的 UGC 域名里查，成本可控（并发，不额外调模型），
    补不到也不阻断 —— 轨迹里能看到它到底是空还是被限制卡住。
    """
    media = [_Retrieval(text=item.query, layer="MEDIA", target=item.target) for item in items]
    if not core:
        return media
    discussion = [
        _Retrieval(
            text=template.format(core=core),
            layer="DISCUSSION",
            target="VOICE",
            options=SearchOptions(
                include_domains=DISCUSSION_DOMAINS,
                time_range=DISCUSSION_TIME_RANGE,
            ),
        )
        for template in DISCUSSION_QUERIES
    ]
    return [*media, *discussion]


def _merge_docs(
    docs: tuple[SearchDoc, ...],
    seen: dict[str, SearchDoc],
    skip: set[str],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """把一批结果并进 ``seen``，同时记下每一条的去留与原因。

    判定顺序就是优先级：上一轮已有 → 本轮重复 → 超出上限 → 采用。丢掉的原因
    必须写清楚，否则「明明搜到 6 条，材料池只多了 1 条」会变成一个无法解释的
    差额 —— 这三种原因的处理方式完全不同（补采要不要换词、上限要不要调）。
    """
    entries: list[dict[str, Any]] = []
    new_count = 0
    for rank, doc in enumerate(docs, 1):
        kind, rule = classify(doc.url, doc.source_label)
        entry: dict[str, Any] = {
            "rank": rank,
            "title": doc.title[:200],
            "url": doc.url,
            "domain": domain_of(doc.url),
            "source_kind": kind,
            "kind_rule": rule,
            "published_at": doc.published_at.isoformat() if doc.published_at else None,
            "kept": False,
            "drop_reason": None,
        }
        if doc.identity in skip:
            entry["drop_reason"] = "EXCLUDED_PREVIOUS_ROUND"
        elif doc.identity in seen:
            entry["drop_reason"] = "DUPLICATE"
        elif len(seen) >= limit:
            entry["drop_reason"] = "OVER_LIMIT"
        else:
            seen[doc.identity] = doc
            entry["kept"] = True
            new_count += 1
        entries.append(entry)
    return entries, new_count


async def _search_all(
    searcher: Searcher,
    retrievals: list[_Retrieval],
    *,
    limit: int = MAX_DOCS,
    exclude: set[str] | None = None,
) -> tuple[list[SearchDoc], list[_RoundTrace]]:
    """检索所有 query，按文档身份去重，并留下每一条 query 的取证记录。

    HTTP 型检索并发跑：一次展开 3~5 条 query，串行等待是这一环最大的空转
    （实测占总耗时 7~12 秒）。能不能并发由 ``searcher.concurrent_safe`` 说了算 ——
    模型端检索要在同一个 run 里占 ``call_seq``，并发会撞唯一索引。

    并发后仍**按 query 顺序合并**：``stored`` 的编号会被立场抽取按
    ``doc_index`` 引用，顺序随网络返回早晚漂移会让同一批材料的编号不可复现。
    轨迹同理按 ``seq`` 排序，不按完成时间。

    ``exclude`` 是上一轮已经拿到的文档身份：补采时不重复喂同一批材料，
    否则抽出来的立场只会和第一轮一模一样，白花一次分析。
    """
    skip = exclude or set()

    if searcher.concurrent_safe and len(retrievals) > 1:
        outcomes = list(await asyncio.gather(*(_search_one(searcher, r) for r in retrievals)))
    else:
        # 串行：模型端检索每次都要记一笔账，顺序还必须是确定的。
        outcomes = [await _search_one(searcher, retrieval) for retrieval in retrievals]

    seen: dict[str, SearchDoc] = {}
    failure: Exception | None = None
    traces: list[_RoundTrace] = []

    for seq, (retrieval, outcome) in enumerate(zip(retrievals, outcomes, strict=True), start=1):
        if isinstance(outcome, SearchError):
            failure = outcome
            traces.append(
                _RoundTrace(
                    seq=seq,
                    retrieval=retrieval,
                    provider=searcher.name,
                    status="ERROR",
                    options=retrieval.options.as_dict(),
                    error=str(outcome)[:500],
                )
            )
            continue

        entries, new_count = _merge_docs(outcome.docs, seen, skip, limit)
        traces.append(
            _RoundTrace(
                seq=seq,
                retrieval=retrieval,
                provider=outcome.provider,
                status="OK" if outcome.docs else "EMPTY",
                latency_ms=outcome.latency_ms,
                result_count=len(outcome.docs),
                new_count=new_count,
                # 以 provider 回传的为准：调用方以为的参数和真正下发的不一定一致。
                options=outcome.options.as_dict(),
                docs=entries,
            )
        )

    if not seen and failure is not None:
        # 一条都没成功：检索不可用，不是「这题没人讨论」。原样抛 → 重试。
        raise failure
    return list(seen.values()), traces


def _source_mix(stored: list[_Doc]) -> dict[str, int]:
    """材料池按来源类型的构成，顺序固定（``SOURCE_KINDS``）以便比对。"""
    counts = Counter(item.source_kind for item in stored)
    return {kind: counts[kind] for kind in SOURCE_KINDS if counts[kind]}


def _merge_mix(*parts: dict[str, int]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for part in parts:
        total.update(part)
    return {kind: total[kind] for kind in SOURCE_KINDS if total[kind]}


async def _record_traces(
    task: TaskContext,
    run_id: UUID,
    *,
    round_no: int,
    traces: list[_RoundTrace],
) -> None:
    """把这一轮的检索记录写进 ``retrieval_traces``。

    写失败只记日志，不阻断展开：轨迹是诊断数据，它坏了不该让一次正常的
    展开跟着失败。反过来，如果为了它放弃整条链，等于用观测手段绑架业务。
    """
    if not traces:
        return
    try:
        async with WorkerSession() as session:
            for trace in traces:
                await session.execute(
                    pg_insert(RetrievalTrace)
                    .values(
                        workspace_id=task.workspace_id,
                        task_id=task.id,
                        run_id=run_id,
                        attempt=task.attempt,
                        round=round_no,
                        seq=trace.seq,
                        layer=trace.retrieval.layer,
                        query=trace.retrieval.text,
                        target=trace.retrieval.target,
                        provider=trace.provider,
                        options=trace.options,
                        status=trace.status,
                        latency_ms=trace.latency_ms,
                        result_count=trace.result_count,
                        new_count=trace.new_count,
                        docs=trace.docs,
                        error=trace.error,
                    )
                    .on_conflict_do_nothing(constraint="retrieval_traces_attempt_seq_key")
                )
            await session.commit()
    except Exception:
        log.exception("agent.trace_write_failed", task_id=str(task.id), round=round_no)


async def _persist_docs(task: TaskContext, docs: list[SearchDoc]) -> list[_Doc]:
    """材料落 ``external_events(SEARCH)``，返回带编号与 id 的列表。

    幂等打在 ``external_events_source_unique`` 上：同一条 URL 第二次进来
    ``ON CONFLICT DO NOTHING``，再查回既有行。重跑不会让材料翻倍。
    """
    stored: list[_Doc] = []
    async with WorkerSession() as session:
        for doc in docs:
            kind, _rule = classify(doc.url, doc.source_label)
            inserted = await session.scalar(
                pg_insert(ExternalEvent)
                .values(
                    workspace_id=task.workspace_id,
                    source_type=SourceType.SEARCH,
                    source_kind=kind,
                    external_id=doc.identity,
                    title=doc.title[:500],
                    summary=doc.snippet,
                    url=doc.url,
                    source_published_at=doc.published_at,
                    raw_payload={
                        **doc.raw,
                        "source_label": doc.source_label,
                        "source_kind_rule": _rule,
                    },
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "source_type", "external_id"]
                )
                .returning(ExternalEvent.id)
            )
            if inserted is None:
                inserted = await session.scalar(
                    select(ExternalEvent.id).where(
                        ExternalEvent.workspace_id == task.workspace_id,
                        ExternalEvent.source_type == SourceType.SEARCH,
                        ExternalEvent.external_id == doc.identity,
                    )
                )
            if inserted is None:
                continue
            stored.append(_Doc(index=len(stored) + 1, doc=doc, event_id=inserted, source_kind=kind))
        await session.commit()
    return stored


# ------------------------------------------------------------------ 立场


def _validate_stances(raw: schemas.StanceSet, stored: list[_Doc]) -> tuple[list[Stance], int]:
    """引文机检：``quote`` 必须是材料片段的子串，否则丢弃。

    没有模型参与，所以可复现。这一条挡的是「模型假装材料里有人这么说」——
    放宽到「意思相近」，等于把这件事交给另一个模型去判，而它恰好最容易被
    相似的措辞骗过。
    """
    by_index = {item.index: item for item in stored}
    accepted: list[Stance] = []
    rejected = 0

    for item in raw.stances:
        target = by_index.get(item.doc_index)
        if target is None:
            rejected += 1
            continue

        quote = squash(item.quote)
        if not quote or quote not in squash(target.doc.snippet):
            rejected += 1
            continue

        accepted.append(
            Stance(
                event_id=target.event_id,
                label=item.label.strip()[:40],
                camp=item.camp,
                claim=item.claim.strip()[:240],
                audience=item.audience.strip()[:120],
                # 存原文里那一段（保留大小写与标点），不是 squash 之后的版本。
                quote=item.quote.strip()[:400],
            )
        )

    return accepted, rejected


async def _supplement(
    task: TaskContext,
    run_id: UUID,
    searcher: Searcher,
    seed_text: str,
    stances: list[Stance],
    missing: list[str],
    *,
    known: set[str],
) -> _Supplement:
    """针对缺失阵营补采一轮，返回新增的立场（不合并，由调用方合并）。

    只有**一次**模型调用：从新材料里抽立场。查询由模板生成 —— 缺口是确定的，
    措辞有没有覆盖到也是确定的，再让模型规划一遍只是多等 4~5 秒。
    ``known`` 里的文档不重复喂，抽立场时只给补采到的材料，所以 ``doc_index``
    天然从 1 开始，不需要换算。
    """
    queries = _supplement_queries(seed_text, missing)
    if not queries:
        return _Supplement(missing_before=tuple(missing))

    try:
        docs, traces = await _search_all(
            searcher,
            [_Retrieval(text=query, layer="SUPPLEMENT") for query in queries],
            limit=MAX_SUPPLEMENT_DOCS,
            exclude=known,
        )
        await _record_traces(task, run_id, round_no=1, traces=traces)
    except SearchError as exc:
        # 第一轮已经拿到了材料，补采整体失败不该把整个任务判死 ——
        # 缺阵营是「信息不足」，不是「这一题不能做」。留在案，不重试。
        log.warning("agent.opinion_supplement_failed", error=str(exc)[:200])
        return _Supplement(queries=tuple(queries), missing_before=tuple(missing))

    if not docs:
        # 补不到新东西不是错误：说明这些声音在公开语料里确实稀薄。
        # 缺口由调用方原样记进 brief，不在这里造数据。
        log.info("agent.opinion_supplement_empty", queries=list(queries))
        return _Supplement(queries=tuple(queries), missing_before=tuple(missing))

    stored = await _persist_docs(task, docs)
    if not stored:
        return _Supplement(queries=tuple(queries), missing_before=tuple(missing))

    stance_step = await runtime.begin_step(run_id)
    raw = await runtime.call_structured(
        run_id,
        purpose=LlmPurpose.ANALYZE,
        step_no=stance_step,
        system=SYSTEM_STANCES_SUPPLEMENT,
        prompt=_stance_prompt(seed_text, stored),
        schema=schemas.StanceSet,
        shape=schemas.STANCE_SET_SHAPE,
        max_output_tokens=MAX_OUTPUT_TOKENS_STANCES,
    )
    accepted, rejected = _validate_stances(raw, stored)
    merged, duplicates = _merge_stances(stances, accepted)

    log.info(
        "agent.opinion_supplement",
        queries=list(queries),
        missing=list(missing),
        docs=len(stored),
        new_stances=len(accepted),
        duplicate_stances=duplicates,
        dropped_stances=rejected,
    )
    return _Supplement(
        queries=tuple(queries),
        docs=len(stored),
        source_mix=_source_mix(stored),
        stances=tuple(merged[len(stances) :]),
        dropped_stances=rejected,
        duplicate_stances=duplicates,
        missing_before=tuple(missing),
    )


#: 种子文本自带的字段标签。``_resolve_seed`` 会拼成「方向：…」「事件：…」，
#: 直接按标点切第一段会把标签本身当成核心词，检索出一堆无关的东西。
_SEED_LABEL = re.compile(r"^(方向|事件|摘要|来源)[：:]\s*")


def _seed_core(seed_text: str) -> str:
    """把种子压成能拼进 query 的短核。

    先剥掉「方向：/事件：」标签，再按标点取第一段 —— 不剥标签会把「方向」
    当成核心词（真发生过：query 变成「方向 确实有用 效果」）。

    按标点切而不是硬截 N 字：硬截会把「效率提升，还是新的形式主义」砍成半句，
    检索就退化成字面匹配。
    """
    text = _SEED_LABEL.sub("", seed_text.strip())
    head = re.split(r"[：:，,。；;？?！!\n]", text, maxsplit=1)[0].strip()
    return (head or text)[:40]


def _supplement_queries(seed_text: str, missing: list[str]) -> list[str]:
    """按缺口生成补采 query，零模型调用。

    顺序稳定、可复现、可单测 —— 这是把「规划」换成「查表」的全部理由：
    缺口本来就是一个确定的事实，不值得再花一次 4~5 秒的调用去问模型。
    """
    core = _seed_core(seed_text)
    if not core:
        return []

    out: list[str] = []
    for camp in missing:
        for template in _SUPPLEMENT_QUERIES.get(camp, ()):
            out.append(template.format(core=core))
            if len(out) >= SUPPLEMENT_MAX_QUERIES:
                return out
    return out


def _missing_camps(stances: list[Stance], *, for_supplement: bool = False) -> list[str]:
    """还没出现的阵营，按 ``ALL_CAMPS`` 的顺序。

    由 Python 算，不经模型 —— 「缺哪一类声音」是集合差，不是判断题。

    ``for_supplement=True`` 时只算 ``SUPPLEMENT_CAMPS``：那是**值得为它多跑
    一轮检索**的缺口。OBSERVER 缺了只是「没有旁观评论」，不构成分歧缺一边。
    """
    universe = SUPPLEMENT_CAMPS if for_supplement else ALL_CAMPS
    present = {item.camp for item in stances}
    return [camp for camp in universe if camp not in present]


def _merge_stances(base: list[Stance], extra: list[Stance]) -> tuple[list[Stance], int]:
    """合并两轮立场，按标签去重（忽略空白与大小写）。

    同一句话被两家媒体转述不该算两条立场：那会让立场池看起来更丰富，
    实际只是同一批声音的副本，候选引用它也不会多出切面。
    """
    seen = {squash(item.label).casefold() for item in base}
    merged = list(base)
    duplicates = 0
    for item in extra:
        key = squash(item.label).casefold()
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        merged.append(item)
    return merged, duplicates


async def _persist_stances(task: TaskContext, stances: list[Stance]) -> list[UUID]:
    """立场落库，返回与 ``stances`` 一一对应的 id。

    同一篇材料上的同一个标签由唯一索引吸收（重跑 upsert），所以返回的 id
    可能重复 —— 调用方按位置取值即可，写关联时再去重。
    """
    ids: list[UUID] = []
    async with WorkerSession() as session:
        for item in stances:
            stmt = pg_insert(OpinionStance).values(
                workspace_id=task.workspace_id,
                seed_task_id=task.id,
                event_id=item.event_id,
                label=item.label,
                camp=item.camp,
                claim=item.claim,
                audience=item.audience,
                quote=item.quote,
            )
            row_id = await session.scalar(
                stmt.on_conflict_do_update(
                    index_elements=["seed_task_id", "event_id", "label"],
                    set_={
                        "camp": stmt.excluded.camp,
                        "claim": stmt.excluded.claim,
                        "audience": stmt.excluded.audience,
                        "quote": stmt.excluded.quote,
                    },
                ).returning(OpinionStance.id)
            )
            if row_id is None:
                # ON CONFLICT DO UPDATE 必然返回行；真没有说明表结构或权限被改过，
                # 不是「这次模型发挥不好」，交给 worker 重试并留下清楚的错误。
                raise RuntimeError(f"立场落库没有返回 id：{item.label!r}")
            ids.append(row_id)
        await session.commit()
    return ids


def _ground(
    result: schemas.GroundedCandidates, stances: list[Stance]
) -> tuple[list[_Grounded], int]:
    """只保留引用了真实立场的候选；引用为空或全部越界的直接丢掉。

    这是「禁止模型先验」的落地点：模型想不出切面时，唯一的选择是从立场池里找。
    """
    kept: list[_Grounded] = []
    dropped = 0
    for candidate in result.candidates:
        indexes = tuple(
            dict.fromkeys(index for index in candidate.stance_indexes if 1 <= index <= len(stances))
        )
        if not indexes:
            dropped += 1
            continue
        kept.append(_Grounded(candidate=candidate, indexes=indexes))
    return kept, dropped


# ------------------------------------------------------------------ 落库


async def _persist(
    task: TaskContext,
    grounded: list[_Grounded],
    stances: list[Stance],
    stance_ids: list[UUID],
    seed_event_id: UUID | None,
    run_id: UUID,
    *,
    provider: str,
    doc_count: int,
    source_mix: dict[str, int],
    supplement: _Supplement,
) -> list[UUID]:
    """候选与它的立场引用一起写入。

    不写 ``primary_event_id``：那是深入之后确定的主证据，
    ``topics_one_per_primary_event`` 是唯一索引，展开阶段占它会让一个事件
    只能展开一个选题 —— 与这一步的目的直接冲突。
    """
    async with WorkerSession() as session:
        rows: list[Topic] = []
        links: list[tuple[Topic, tuple[int, ...]]] = []

        for item in grounded:
            candidate = item.candidate
            cited = [stances[index - 1] for index in item.indexes]
            topic = Topic(
                workspace_id=task.workspace_id,
                seed_task_id=task.id,
                seed_event_id=seed_event_id,
                title=candidate.title[:500],
                description=f"{candidate.angle}｜{candidate.audience}",
                status=TopicStatus.CANDIDATE,
                brief={
                    "stage": "EXPANDED",
                    "angle": candidate.angle,
                    "audience": candidate.audience,
                    "thesis": candidate.thesis,
                    "why_now": candidate.why_now,
                    # 前端要展示「这个切面建立在什么之上」，所以留一份精简副本；
                    # 完整证据（quote + 出处）在 opinion_stances 里。
                    "stances": [
                        {
                            "label": stance.label,
                            "camp": stance.camp,
                            "claim": stance.claim,
                            "audience": stance.audience,
                        }
                        for stance in cited
                    ],
                    "opinion": {"checked": False},
                    "seed_grounded": True,
                    "sources": {
                        "provider": provider,
                        "doc_count": doc_count,
                        "stance_count": len(stances),
                        # 「这些立场是谁说的」：全 MEDIA 时 camp_counts 只是
                        # 几篇文章站哪边，不是多少人站哪边。
                        "source_mix": source_mix,
                        # 补采留痕：补了几轮、补了什么、补完还缺什么。
                        # 缺口的最终结论在 check_opinion 的 coverage 里再算一遍
                        # （那里读的是库里全部立场，含补采新增的）。
                        "supplement": {
                            "rounds": 1 if supplement.ran else 0,
                            "queries": list(supplement.queries),
                            "new_docs": supplement.docs,
                            "new_stances": len(supplement.stances),
                            "duplicate_stances": supplement.duplicate_stances,
                            "missing_before": list(supplement.missing_before),
                            "missing_after": list(supplement.missing_after),
                        },
                    },
                    "run_id": str(run_id),
                },
            )
            session.add(topic)
            rows.append(topic)
            links.append((topic, item.indexes))

        await session.flush()

        stance_rows: list[dict[str, Any]] = []
        for topic, indexes in links:
            seen_ids: set[UUID] = set()
            for position, index in enumerate(indexes):
                stance_id = stance_ids[index - 1]
                if stance_id in seen_ids:
                    continue
                seen_ids.add(stance_id)
                stance_rows.append(
                    {
                        "workspace_id": task.workspace_id,
                        "topic_id": topic.id,
                        "stance_id": stance_id,
                        # 第一个引用是主立场：写作时需要知道「这篇主要回应的
                        # 是哪种声音」，其余是支撑。
                        "is_primary": position == 0,
                    }
                )
        if stance_rows:
            await session.execute(pg_insert(TopicStance).values(stance_rows))

        await session.commit()
        return [row.id for row in rows]


async def _enqueue_opinion_check(task: TaskContext) -> UUID | None:
    """把舆论检测排进队列。``dedup_key`` 让展开重跑不会排第二遍。"""
    async with WorkerSession() as session:
        created = await create_task(
            session,
            workspace_id=task.workspace_id,
            payload=TaskCreate(
                task_type="check_opinion",
                input={"seed_task_id": str(task.id)},
                assigned_agent="opinion",
                dedup_key=f"{task.id}:opinion",
            ),
        )
    return created.id


# ------------------------------------------------------------------ 取数


async def _resolve_seed(task: TaskContext, seed_type: str) -> tuple[str, UUID | None]:
    """把「事件」或「方向」统一成一段种子文本。

    事件型要回查库：既确认它存在，也确认它属于本租户 —— worker 是 BYPASSRLS，
    写错 workspace_id 不会被策略挡住。方向型不需要回查，文本就是全部输入。
    """
    if seed_type == "event":
        raw = task.input.get("event_id")
        if not raw:
            raise NonRetryableError("seed_type=event 需要 input.event_id", code="MISSING_EVENT_ID")
        event_id = UUID(str(raw))

        async with WorkerSession() as session:
            event = await session.scalar(
                select(ExternalEvent).where(
                    ExternalEvent.id == event_id,
                    ExternalEvent.workspace_id == task.workspace_id,
                )
            )
        if event is None:
            raise NonRetryableError(
                f"事件不存在或不属于当前工作区: {event_id}", code="EVENT_NOT_FOUND"
            )

        lines = [f"事件：{event.title}"]
        if event.summary:
            lines.append(f"摘要：{event.summary}")
        if event.url:
            lines.append(f"来源：{event.url}")
        return "\n".join(lines), event.id

    direction = str(task.input.get("direction") or "").strip()
    if not direction:
        raise NonRetryableError(
            "seed_type=direction 需要 input.direction", code="MISSING_DIRECTION"
        )
    return f"方向：{direction}", None


async def _existing_topics(task: TaskContext) -> list[UUID]:
    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(Topic.id)
                .where(
                    Topic.workspace_id == task.workspace_id,
                    Topic.seed_task_id == task.id,
                )
                .order_by(Topic.created_at)
            )
        ).scalars()
        return list(rows)


async def _active_strategy_config(workspace_id: UUID) -> dict[str, Any] | None:
    """当前生效战略的配置。

    直接 JOIN ``strategy_versions`` 而不是走 ``Strategy.current_version``
    关系：异步会话里访问未 eager load 的关系会抛 ``MissingGreenlet``。
    """
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(StrategyVersion.strategy_config)
                .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
                .where(
                    Strategy.workspace_id == workspace_id,
                    Strategy.status == StrategyStatus.ACTIVE,
                    StrategyVersion.version_number == Strategy.current_version_number,
                )
                .limit(1)
            )
        ).first()

    if row is None:
        # 没有战略不是错误：候选仍然成立，只是相关度打分没有依据。
        return None
    return dict(row[0] or {})


def _plan_prompt(seed_text: str) -> str:
    return f"{seed_text}\n\n请给出采集公众讨论的检索需求。"


def _stance_prompt(seed_text: str, stored: list[_Doc]) -> str:
    lines = [seed_text, "", "检索材料："]
    for item in stored[:DOCS_IN_PROMPT]:
        lines.append(
            f"[{item.index}] {item.doc.title[:100]}\n"
            f"    来源：{item.doc.source}\n"
            f"    原文：{squash(item.doc.snippet)[:MAX_CHARS_PER_DOC]}"
        )
    return "\n".join(lines)


def _candidate_prompt(
    seed_text: str,
    count: int,
    stances: list[Stance],
    strategy: dict[str, Any] | None,
) -> str:
    lines = [seed_text, "", "立场池："]
    for index, stance in enumerate(stances, start=1):
        lines.append(
            f"[{index}] {stance.label}（{stance.camp}）\n"
            f"    主张：{stance.claim}\n"
            f"    持有者：{stance.audience}"
        )
    lines.append("")
    lines.append(f"请给出 {count} 个候选选题，每个必须引用上面的立场序号。")
    if strategy:
        lines.append(
            "账号定位（战略配置，写出符合这个定位的受众与调性）：\n"
            + json.dumps(strategy, ensure_ascii=False)[:1500]
        )
    return "\n".join(lines)


def _clamp(raw: int) -> int:
    return max(MIN_COUNT, min(MAX_COUNT, raw))


def _max_cost(task: TaskContext) -> Decimal:
    raw = task.input.get("max_cost")
    return Decimal(str(raw)) if raw is not None else Decimal("0.9")


__all__ = ["AGENT_NAME", "Stance", "expand_topics"]
