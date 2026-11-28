"""深入：把「一个角度」变成「一份能写的 brief + 一组可核的证据」。

这是整条链上唯一必须联网的一环。展开可以靠模型先验（切面是它擅长的事），
写作可以靠 brief（结构已经定了），但**深入要回答的是「事实是什么」**——
这件事模型没有能力凭空给出，尤其在它不知道今年发生了什么的题材上。

## 三步，而不是一次调用

1. **定检索计划**：先把「要查什么」写成 2~5 条带目的的 query。要求至少一条
   COUNTER（专门找反例）—— 只查支持性材料的检索，必然把选题写成一个
   没人反对的常识。
2. **检索**：每条 query 打一次真实检索，结果落 ``external_events(SEARCH)``。
   落库而不是留在内存，是因为证据必须能被审核、被复用、被反查；
   顺便让重跑去重（唯一键在 ``(workspace, source_type, external_id)``）。
3. **抽证据 + 合成 brief**：一次结构化调用拿回 thesis / outline /
   counterpoints / evidence。``quote`` 落库前做子串机检，模型改写过的引文
   直接丢掉 —— 不依赖 LLM judge，这也是唯一挡得住「编引文」的办法。

## 失败与降级

- 检索全失败 → 抛异常交给 worker 重试。搜不到就写，等于把整条链退回
  「模型凭记忆编」，那正是这一环存在的理由。
- 证据一条都没通过机检 → 不失败，改用检索摘要本身当 BACKGROUND 证据，
  并在 ``brief.search.degraded`` 标出来。降级必须是可见的。
- ``search_provider=stub`` → ``degraded=True``。测试环境走这条，但库里
  一眼能看出这条内容的事实依据是假的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from xing.agents import runtime, schemas
from xing.core.config import settings
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.event import ExternalEvent, SourceType
from xing.models.runtime import LlmPurpose
from xing.models.topic import Topic, TopicEvent, TopicStatus
from xing.schemas.task import TaskCreate
from xing.search import (
    SearchConfigError,
    SearchDoc,
    Searcher,
    SearchError,
    build_searcher,
    squash,
)
from xing.services.task_service import create_task
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "deepener"

#: 进 prompt 的材料条数。再多只是把输出预算摊薄 —— 模型不会因为看到第 20 条
#: 就多写一个论点，只会把每一条都写得更浅。
MAX_DOCS_IN_PROMPT = 12
#: 每条材料进 prompt 的字符上限。校验用的是完整片段，不是这个截断版。
MAX_CHARS_PER_DOC = 600
#: 一题最多收集多少条材料，成本闸门。
MAX_DOCS = 16

#: 角色 → 权重。权重只表达「这条证据有多硬」，不是模型打的分：
#: 直接支撑论点的原始报道最硬，背景材料最软。
ROLE_WEIGHT: dict[str, Decimal] = {
    "PRIMARY": Decimal("1.0"),
    "CONTRADICTING": Decimal("0.9"),
    "SUPPORTING": Decimal("0.7"),
    "BACKGROUND": Decimal("0.3"),
}

SYSTEM_QUERIES = """你是 XING 的检索规划 Agent。给定一个选题，写出用来核查它的事实材料的检索需求。

硬性要求：
1. 2~5 条 query，每条只查一件事。不要写「XX 的背景与现状」这种大而全的句子。
2. **至少一条 purpose=COUNTER**：专门去找与论点相反的事实、失败案例、反例。
   找不到反例的选题不值得写。
3. 至少一条 purpose=DATA：具体的数字、比例、金额、时间。
4. query 要像人在搜索框里输入的话，带主体和时间限定（如「2026年」），
   不要用「探讨」「分析」这类没有检索价值的词。
5. purpose 取值：FACT（事实核查）/ COUNTER（反例）/ DATA（数据）/ BACKGROUND（背景）。

只输出 JSON：{"queries": [{"query": "...", "purpose": "FACT"}]}
不要输出解释、不要 Markdown 代码块。"""

SYSTEM_DIGEST = """你是 XING 的内容策划 Agent。你会看到一批带编号的检索材料，以及一个选题。
据此写出一份可以直接交给写作者的 brief，并挑出支撑它的证据。

要求：
1. thesis：一句可被反驳的判断。不要复述选题标题，不要把「很重要」当论点。
2. differentiator：这篇内容与同类内容的不同之处。写不出来说明选题没有价值。
3. outline：3~6 条。每条是一个「要讲清的点」，不是「第一部分」「背景介绍」这种空壳。
4. counterpoints：1~3 条 —— 这个论点最容易被怎么反驳。想不出反例的论点通常是废话。
   **每个元素只能是一个字符串**（一句话）：不能是对象，不能写成 "反驳点": "..."，
   也不能把 evidence 放进这个数组 —— counterpoints 和 evidence 是**平级**的两个键。
5. evidence：3~6 条，挑最能撑住判断的。两条够用就写两条，不要为凑数稀释。
   - doc_index：材料编号。
   - quote：**逐字复制**材料里的原句，不超过 120 字。
     不要用省略号把两处拼接起来，不要改写、不要翻译 —— 引文会在落库前做子串机检，
     接不上原文的整条会被丢弃。宁可短一句，也不要长而拼不成原句。
   - role：PRIMARY 直接支撑论点 / SUPPORTING 补充 / CONTRADICTING 反例 / BACKGROUND 背景。
   - claim：用你自己的话说明这条证据证明了什么，一句话，不超过 60 字。
6. 材料里没有的事实、数字、机构名，一律不要写进 brief。

只输出**一个** JSON 对象，五个键平级，顺序照抄：
{"thesis": "...", "differentiator": "...", "outline": ["..."],
 "counterpoints": ["..."],
 "evidence": [{"doc_index": 1, "role": "PRIMARY", "quote": "...", "claim": "..."}]}
不要输出解释、不要 Markdown 代码块、不要在字符串里直接换行。"""


@dataclass(frozen=True, slots=True)
class _Doc:
    """一条材料 + 它落库后的 id + 在 prompt 里的编号。"""

    index: int
    doc: SearchDoc
    event_id: UUID


@dataclass(frozen=True, slots=True)
class Evidence:
    event_id: UUID
    role: str
    weight: Decimal
    quote: str
    claim: str


@handler("deepen_topic")
async def deepen_topic(task: TaskContext) -> dict[str, Any]:
    raw = task.input.get("topic_id")
    if not raw:
        raise NonRetryableError("deepen_topic 需要 input.topic_id", code="MISSING_TOPIC_ID")
    topic_id = UUID(str(raw))

    topic = await _load_topic(task, topic_id)
    if _is_deepened(topic):
        # 已经深入过就不再搜、不再生成。但仍要确保「写 + 审」已经排上 ——
        # brief 落库与排产是两次提交，中间崩溃会让重跑停在这里，
        # 而链条会在「有 brief 但没人写」这个状态上永远停住。
        chain = await _enqueue_generation(task, topic, topic.brief or {})
        log.info("agent.deepen_reused", task_id=str(task.id), topic_id=str(topic_id))
        return {
            "topic_id": str(topic_id),
            "stage": "DEEPENED",
            "reused": True,
            "evidence_count": len((topic.brief or {}).get("evidence_refs") or []),
            **chain,
        }

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=f"深入选题：{topic.title}"[:2000],
            task_id=task.id,
            attempt=task.attempt,
            max_cost=_max_cost(task),
            max_steps=5,
        )
    )

    try:
        plan_step = await runtime.begin_step(run_id)
        plan = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.PLAN,
            step_no=plan_step,
            system=SYSTEM_QUERIES,
            prompt=_plan_prompt(topic),
            schema=schemas.SearchPlan,
            shape=schemas.SEARCH_PLAN_SHAPE,
            max_output_tokens=600,
        )

        try:
            searcher = build_searcher(run_id=run_id, step_no=plan_step)
        except SearchConfigError as exc:
            # 配置错重试不会变对。终态并带上明确 code，而不是烧三次预算。
            raise NonRetryableError(str(exc), code="SEARCH_NOT_CONFIGURED") from exc
        docs = await _search_all(searcher, plan)
        if not docs:
            # 一条都没搜到：这是「这个题在公开信息里没有落点」，
            # 换一版 query 重试也没用，交给上层决定换题还是换说法。
            raise NonRetryableError(
                "检索没有返回任何材料，无法建立事实基线", code="NO_SEARCH_RESULT"
            )

        stored = await _persist_docs(task, docs)
        if not stored:
            raise NonRetryableError("检索材料落库失败", code="EVIDENCE_STORE_FAILED")

        digest_step = await runtime.begin_step(run_id)
        digest = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.GENERATE,
            step_no=digest_step,
            system=SYSTEM_DIGEST,
            prompt=_digest_prompt(topic, stored),
            schema=schemas.DeepenResult,
            shape=schemas.DEEPEN_SHAPE,
            # 4000 是余量，不是结论：实测这一版深挖的真实用量约 1500 tokens
            # （9 条证据、引文五六百字）。之前设 2000 时我一度以为是截断 ——
            # 查了 finish_reason 才发现是 "stop"，输出是完整的，坏在别处。
            # 真正的毛病是模型写长中文值时漏引号（``"claim":`` 换行后直接跟裸文本）。
            # 那条由下面的 ``_repair_json`` 兜底；这里只保证预算不会先成为瓶颈。
            max_output_tokens=4000,
        )

        evidence, rejected = _validate(digest, stored)
        if not evidence:
            # 全部引文都不合法：不是「模型这次发挥不好」，是它没在照抄。
            # 用材料原文兜底成 BACKGROUND，并把降级写进 brief —— 有证据但不够硬，
            # 比一个假装成功的 DEEPENED 诚实。
            evidence = _fallback(stored)
            log.warning(
                "agent.deepen_evidence_degraded",
                topic_id=str(topic_id),
                rejected=rejected,
            )

        brief = _build_brief(
            topic=topic,
            digest=digest,
            evidence=evidence,
            plan=plan,
            provider=searcher.name,
            doc_count=len(stored),
            run_id=run_id,
            rejected=rejected,
        )
        await _write_evidence(task, topic_id, evidence)
        await _write_brief(task, topic, brief, evidence)
        chain = await _enqueue_generation(task, topic, brief)

        await runtime.finish_run(
            run_id,
            output={
                "topic_id": str(topic_id),
                "evidence_count": len(evidence),
                "doc_count": len(stored),
                "provider": searcher.name,
                "write_task_id": chain["write_task_id"],
                "review_task_id": chain["review_task_id"],
            },
        )
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    log.info(
        "agent.deepened",
        task_id=str(task.id),
        topic_id=str(topic_id),
        docs=len(stored),
        evidence=len(evidence),
        provider=searcher.name,
    )
    return {
        "topic_id": str(topic_id),
        "stage": "DEEPENED",
        "evidence_count": len(evidence),
        "doc_count": len(stored),
        "provider": searcher.name,
        "run_id": str(run_id),
        "write_task_id": chain["write_task_id"],
    }


# ------------------------------------------------------------------ 检索


async def _search_all(searcher: Searcher, plan: schemas.SearchPlan) -> list[SearchDoc]:
    """逐条检索并按文档身份去重。

    串行而不是并发：模型端检索走 ``runtime.call_model``，而 ``call_seq`` 是
    ``max+1`` 求出来的 —— 同一个 run 上并发插入会撞唯一索引。串行的代价是
    延迟，但检索本来就是这一环最慢的部分，并发省下的那点时间不值得为它
    在账本上开一个并发写入口。

    单条失败继续：一次 429 不该让整道题作废，只要还有材料就能往下走。
    """
    seen: dict[str, SearchDoc] = {}
    failure: Exception | None = None

    for item in plan.queries:
        try:
            hit = await searcher.search(item.query, max_results=_max_results())
        except SearchError as exc:
            failure = exc
            log.warning("agent.search_failed", query=item.query, error=str(exc)[:200])
            continue

        for doc in hit.docs:
            if doc.identity in seen:
                continue
            seen[doc.identity] = doc
            if len(seen) >= MAX_DOCS:
                return list(seen.values())

    if not seen and failure is not None:
        # 一条都没成功：这不是「内容问题」，是检索不可用。原样抛出 → worker 重试。
        raise failure
    return list(seen.values())


async def _persist_docs(task: TaskContext, docs: list[SearchDoc]) -> list[_Doc]:
    """材料落 ``external_events(SEARCH)``，返回带编号与 id 的列表。

    幂等打在 ``external_events_source_unique`` 上：同一条 URL 第二次进来
    ``ON CONFLICT DO NOTHING``，再查回既有行。重跑不会让证据翻倍，
    也不会因为「已存在」就跳过 —— 两条路径合一。
    """
    stored: list[_Doc] = []
    async with WorkerSession() as session:
        for doc in docs:
            inserted = await session.scalar(
                pg_insert(ExternalEvent)
                .values(
                    workspace_id=task.workspace_id,
                    source_type=SourceType.SEARCH,
                    external_id=doc.identity,
                    title=doc.title[:500],
                    summary=doc.snippet,
                    url=doc.url,
                    source_published_at=doc.published_at,
                    raw_payload={**doc.raw, "source_label": doc.source_label},
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
            stored.append(_Doc(index=len(stored) + 1, doc=doc, event_id=inserted))
        await session.commit()
    return stored


# ------------------------------------------------------------------ 证据


def _validate(digest: schemas.DeepenResult, stored: list[_Doc]) -> tuple[list[Evidence], int]:
    """引文机检：``quote`` 必须是材料片段的子串，否则丢弃。

    这一步没有模型参与，所以结论可复现。放宽到「意思相近」就等于把
    「编引文」这件事交给另一个模型去判，而它恰好最容易被相似的措辞骗过。
    """
    by_index = {item.index: item for item in stored}
    accepted: list[Evidence] = []
    seen: set[tuple[UUID, str]] = set()
    rejected = 0

    for item in digest.evidence:
        target = by_index.get(item.doc_index)
        if target is None:
            rejected += 1
            continue

        quote = squash(item.quote)
        if not quote or quote not in squash(target.doc.snippet):
            rejected += 1
            continue

        key = (target.event_id, quote[:80])
        if key in seen:
            continue
        seen.add(key)

        accepted.append(
            Evidence(
                event_id=target.event_id,
                role=item.role,
                weight=ROLE_WEIGHT[item.role],
                # 存原文里那一段（保留大小写与标点），不是 squash 之后的版本。
                quote=item.quote.strip()[:600],
                claim=item.claim.strip()[:300],
            )
        )

    return accepted, rejected


def _fallback(stored: list[_Doc]) -> list[Evidence]:
    """引文全废时的兜底：拿材料原文自己当证据。

    这不是「凑数」—— 引文变成材料片段本身，没有任何模型改写，可信度反而
    不低于模型摘的那句。问题只是它没有经过筛选，所以角色只能给 BACKGROUND。
    """
    return [
        Evidence(
            event_id=item.event_id,
            role="BACKGROUND",
            weight=ROLE_WEIGHT["BACKGROUND"],
            quote=item.doc.snippet[:600],
            claim=item.doc.title[:300],
        )
        for item in stored[:3]
    ]


async def _write_evidence(task: TaskContext, topic_id: UUID, evidence: list[Evidence]) -> None:
    """证据写 ``topic_events``。同一事件重跑时更新角色/原文，不新增行。

    必须先按 ``event_id`` 去重再写：模型会把同一篇材料同时当支撑和反驳
    （真实跑出现过 —— 一篇讲「认知差距」的文章，两段话各支持一边）。
    Postgres 不允许一条 INSERT 里 ON CONFLICT 命中同一行两次，整条语句会直接
    报 ``cannot affect row a second time``，于是深入这一步白跑一次重试。
    """
    if not evidence:
        return
    rows = [
        {
            "topic_id": topic_id,
            "event_id": item.event_id,
            "role": item.role,
            "weight": item.weight,
            "quote": item.quote,
        }
        for item in _dedupe_evidence(evidence)
    ]
    async with WorkerSession() as session:
        stmt = pg_insert(TopicEvent).values(rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["topic_id", "event_id"],
                set_={
                    "role": stmt.excluded.role,
                    "weight": stmt.excluded.weight,
                    "quote": stmt.excluded.quote,
                },
            )
        )
        await session.commit()


async def _write_brief(
    task: TaskContext, topic: Topic, brief: dict[str, Any], evidence: list[Evidence]
) -> None:
    """brief 与主证据一起写回。

    ``primary_event_id`` 上有 ``topics_one_per_primary_event`` 唯一索引：
    一个事件只能是一个选题的主证据。被占用时**不报错**，留 NULL ——
    证据已经挂在 ``topic_events`` 上了，少一个「主」字不影响可核性，
    而报错会让整道题白跑。
    """
    primary = _pick_primary(evidence)
    async with WorkerSession() as session:
        values: dict[str, Any] = {"brief": brief}
        if primary is not None:
            taken = await session.scalar(
                select(Topic.id).where(
                    Topic.workspace_id == task.workspace_id,
                    Topic.primary_event_id == primary,
                    Topic.id != topic.id,
                )
            )
            if taken is None:
                values["primary_event_id"] = primary
        try:
            await session.execute(
                update(Topic)
                .where(Topic.id == topic.id, Topic.workspace_id == task.workspace_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        except IntegrityError:
            # 并发下另一个选题刚好认领了同一事件。退一步只写 brief。
            await session.rollback()
            await session.execute(
                update(Topic)
                .where(Topic.id == topic.id, Topic.workspace_id == task.workspace_id)
                .values(brief=brief)
                .execution_options(synchronize_session=False)
            )
            await session.commit()


#: 一条材料被赋予多个角色时，保留谁。数字越小越强。
#: PRIMARY 是直接支撑论点的原话，丢了它这张题就没有主证据。
ROLE_PRIORITY: dict[str, int] = {
    "PRIMARY": 0,
    "CONTRADICTING": 1,
    "SUPPORTING": 2,
    "BACKGROUND": 3,
}


def _dedupe_evidence(evidence: list[Evidence]) -> list[Evidence]:
    """同一篇材料只保留一个角色，顺序按首次出现。

    判据是「这条证据能证明什么」而不是「模型说了几次」：一篇材料里既有支撑
    又有反驳时，先留下更强的那个角色；同强度则保留先出现的引文（模型的排序
    通常就是相关性排序）。
    """
    best: dict[UUID, Evidence] = {}
    for item in evidence:
        current = best.get(item.event_id)
        if current is None or ROLE_PRIORITY.get(item.role, 9) < ROLE_PRIORITY.get(current.role, 9):
            best[item.event_id] = item
    return list(best.values())


def _pick_primary(evidence: list[Evidence]) -> UUID | None:
    """最强的那条 PRIMARY；没有 PRIMARY 就用最重的非反例证据。

    只有 CONTRADICTING / BACKGROUND 时不设主证据：把反例当「主证据」会让
    下游误以为论点有直接支撑。
    """
    primaries = [item for item in evidence if item.role == "PRIMARY"]
    pool = primaries or [item for item in evidence if item.role in ("SUPPORTING", "BACKGROUND")]
    if not pool:
        return None
    return max(pool, key=lambda item: item.weight).event_id


def _build_brief(
    *,
    topic: Topic,
    digest: schemas.DeepenResult,
    evidence: list[Evidence],
    plan: schemas.SearchPlan,
    provider: str,
    doc_count: int,
    run_id: UUID,
    rejected: int,
) -> dict[str, Any]:
    """合并成 ``stage=DEEPENED`` 的完整 brief。

    保留 expand 写的 angle/audience/why_now：它们解释「为什么是这个角度」，
    deepen 只补充「这个角度的事实与结构」，不覆盖。
    """
    base = dict(topic.brief or {})
    return {
        **base,
        "stage": "DEEPENED",
        "thesis": digest.thesis,
        "differentiator": digest.differentiator,
        "outline": list(digest.outline),
        "counterpoints": list(digest.counterpoints),
        "evidence_refs": [
            {"event_id": str(item.event_id), "role": item.role, "weight": float(item.weight)}
            for item in evidence
        ],
        "search": {
            "provider": provider,
            "queries": [{"query": q.query, "purpose": q.purpose} for q in plan.queries],
            "doc_count": doc_count,
            "evidence_count": len(evidence),
            "rejected_quotes": rejected,
            # stub 检索器的每一条材料都是假的，这里必须留痕。
            "degraded": provider == "stub",
            "run_id": str(run_id),
        },
        "deepened_at": datetime.now(UTC).isoformat(),
    }


# ------------------------------------------------------------------ 排产


async def _enqueue_generation(
    task: TaskContext, topic: Topic, brief: dict[str, Any]
) -> dict[str, str]:
    """深入完成后把「写 + 审」排进队列。

    为什么排产放在这里而不是再引一个 orchestrator：这条链从深入到成稿是
    一对一直链（深入 → 写 → 审），结构没有分叉，也没有模型需要决定的步骤。
    ``plan_content`` 之所以有 orchestrator，是因为它从一句 goal 出发，
    「分几步、每步做什么」本身需要判断；这里不需要。
    """
    text = render_brief(topic.title, brief)
    common = {
        "topic_id": str(topic.id),
        "actor_id": task.input.get("actor_id"),
        "content_type": task.input.get("content_type"),
    }
    common = {key: value for key, value in common.items() if value is not None}

    async with WorkerSession() as session:
        write = await create_task(
            session,
            workspace_id=task.workspace_id,
            payload=TaskCreate(
                task_type="write_content",
                input={**common, "title": topic.title, "brief": text},
                assigned_agent="writer",
                dedup_key=f"{topic.id}:write",
            ),
        )
        review = await create_task(
            session,
            workspace_id=task.workspace_id,
            payload=TaskCreate(
                task_type="review_content",
                depends_on=[write.id],
                input={
                    **common,
                    "focus": (
                        "事实是否都能在证据里找到出处；有没有把证据的结论强度放大；"
                        "论点是否被反例拆掉却没有回应。"
                    ),
                    "upstream_task_id": str(write.id),
                },
                assigned_agent="reviewer",
                dedup_key=f"{topic.id}:review",
            ),
        )
    return {"write_task_id": str(write.id), "review_task_id": str(review.id)}


def render_brief(title: str, brief: dict[str, Any]) -> str:
    """把结构化 brief 渲染成写作者读的那段话。

    渲染而不是把 JSON 塞给模型：写作者需要的是「写什么」，不是「字段名是什么」。
    JSON 会让它开始复述键名，而 outline 的层级关系反而丢了。
    """
    lines = [f"标题：{title}"]
    for key, label in (
        ("angle", "切入角度"),
        ("audience", "目标读者"),
        ("thesis", "核心论点"),
        ("differentiator", "与同类内容的不同"),
        ("why_now", "为什么现在写"),
    ):
        value = str(brief.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")

    outline = [str(item).strip() for item in brief.get("outline") or [] if str(item).strip()]
    if outline:
        lines.append("结构：")
        lines.extend(f"{index}. {item}" for index, item in enumerate(outline, start=1))

    counterpoints = [
        str(item).strip() for item in brief.get("counterpoints") or [] if str(item).strip()
    ]
    if counterpoints:
        lines.append("必须回应的反例：")
        lines.extend(f"- {item}" for item in counterpoints)

    refs = brief.get("evidence_refs") or []
    if refs:
        lines.append(
            f"证据：系统会附上 {len(refs)} 条带编号的检索材料。"
            "只能用这些材料里的事实与数字，引用时标 [编号]；材料里没有的一律不写。"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ 取数


async def _load_topic(task: TaskContext, topic_id: UUID) -> Topic:
    async with WorkerSession() as session:
        topic = await session.scalar(
            select(Topic).where(Topic.id == topic_id, Topic.workspace_id == task.workspace_id)
        )
    if topic is None:
        # 交叉租户与不存在共用同一个错误：worker 是 BYPASSRLS，这条查询是唯一防线。
        raise NonRetryableError(f"选题不存在或不属于当前工作区: {topic_id}", code="TOPIC_NOT_FOUND")
    if topic.status not in (*TopicStatus.SELECTABLE, TopicStatus.SELECTED):
        raise NonRetryableError(f"选题状态为 {topic.status}，不能深入", code="TOPIC_NOT_DEEPENABLE")
    return topic


def _is_deepened(topic: Topic) -> bool:
    return (topic.brief or {}).get("stage") == "DEEPENED"


def _plan_prompt(topic: Topic) -> str:
    brief = topic.brief or {}
    parts = [f"选题：{topic.title}"]
    for key, label in (
        ("angle", "角度"),
        ("audience", "受众"),
        ("thesis", "暂定论点"),
        ("why_now", "时效理由"),
    ):
        value = str(brief.get(key) or "").strip()
        if value:
            parts.append(f"{label}：{value}")
    parts.append("请给出检索需求。")
    return "\n".join(parts)


def _digest_prompt(topic: Topic, stored: list[_Doc]) -> str:
    brief = topic.brief or {}
    lines = [f"选题：{topic.title}"]
    if brief.get("angle"):
        lines.append(f"角度：{brief['angle']}")
    if brief.get("audience"):
        lines.append(f"受众：{brief['audience']}")
    lines.append("")
    lines.append("检索材料：")
    for item in stored[:MAX_DOCS_IN_PROMPT]:
        snippet = squash(item.doc.snippet)[:MAX_CHARS_PER_DOC]
        lines.append(
            f"[{item.index}] {item.doc.title[:120]}\n"
            f"    来源：{item.doc.source}\n"
            f"    原文：{snippet}"
        )
    return "\n".join(lines)


def _max_results() -> int:
    return max(1, min(settings.search_max_results, 8))


def _max_cost(task: TaskContext) -> Decimal:
    raw = task.input.get("max_cost")
    return Decimal(str(raw)) if raw is not None else Decimal("0.8")


__all__ = ["AGENT_NAME", "deepen_topic", "render_brief"]
