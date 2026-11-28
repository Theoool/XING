"""舆论检测：候选到底有没有人真的在讨论，切面和受众是不是真的不同。

它是闸门，不是建议。放在独立 run 里，理由和打分一样：同一个上下文里让模型
检查自己刚生成的候选，结论一定偏向「都挺好」。这里它只看到**立场池 + 候选**，
看不到生成时的推理过程。

两条判定线，一条机检一条模型判：

- **机检（无模型）**：两个候选引用了完全相同的立场集合、又写给完全相同的
  受众 —— 无论措辞怎么换，它们是一条。直接降为 WEAK。
- **模型判**：这个切面在立场池里有没有明确持有者（还是把个别说法放大成主流）、
  目标群体是否可识别、与其它候选是否重叠。

**不判真假。** 事实是否成立是 ``deepen_topic`` 的事；这一环只回答
「这个题有没有人真的在争」，否则会把「我不认同这个观点」误判成「这个观点
不存在」。

结论落 ``topics.brief.opinion``，不合格的候选置 ``REJECTED`` 并保留原因 ——
被拒的候选仍然可见，只是不可选。这比悄悄从列表里删掉诚实。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update

from xing.agents import runtime, schemas
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.event import ExternalEvent
from xing.models.runtime import LlmPurpose
from xing.models.topic import OpinionStance, Topic, TopicStance, TopicStatus
from xing.schemas.task import TaskCreate
from xing.search import sources
from xing.search.sources import SOURCE_KINDS
from xing.services.task_service import create_task
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "opinion_checker"

MAX_OUTPUT_TOKENS = 1800
#: 检测最多看多少个候选。展开上限是 8，留一点余量。
MAX_CANDIDATES = 8
#: 每个候选最多展示几条立场。再多只是把 prompt 摊薄。
MAX_STANCES_PER_CANDIDATE = 4

CAMP_ORDER = ("SUPPORT", "OPPOSE", "CONDITIONAL", "EXPERIENCE", "OBSERVER")
CAMP_LABEL = {
    "SUPPORT": "支持",
    "OPPOSE": "反对",
    "CONDITIONAL": "有条件同意",
    "EXPERIENCE": "亲历者",
    "OBSERVER": "旁观分析",
}

#: 来源类型的中文说法。只用于 prompt 与界面，不参与判定。
SOURCE_KIND_LABEL = {
    "MEDIA": "媒体",
    "UGC": "用户发言",
    "OFFICIAL": "官方机构",
    "VESTED": "利益相关方",
    "UNKNOWN": "未分类",
}

SYSTEM_PROMPT = """你是 XING 的舆论检测 Agent。你会看到一个立场池（从真实检索材料
里抽出来的公众观点），以及一批候选选题。判断每个候选**是否真的建立在舆论之上**。

判定标准（只判舆论，不判真假）：
- GROUNDED：它回应的分歧在立场池里有明确持有者；目标受众具体可识别；
  与其它候选的切面和人群确实不同。
- WEAK：立场池里有相关声音，但目标受众模糊（「大众」「企业」这类），
  或与另一候选高度重叠（同一批人、同一个分歧，只是换了措辞）。
- UNGROUNDED：立场池里根本没有这个切面；或把个别人的说法放大成主流；
  或受众在立场池里找不到对应群体。

必须遵守：
1. **不要判断事实真假、不要判断观点对错。** 观点是否正确、数据是否真实，
   由后面的深入与审核环节负责。这一环只回答「有没有人真的这么说」。
2. 不要因为某个观点「不正确」「不道德」「有争议」就判 UNGROUNDED ——
   有争议恰恰说明它值得写。
3. overlap_with 填与它重叠的候选序号（1 开始）；没有就给空数组。
4. reasons 要能复核：指到具体是哪条立场、哪个人群对不上。

只输出 JSON：
{"verdicts": [{"candidate_index": 1, "verdict": "GROUNDED",
               "audience_identified": true, "overlap_with": [], "reason": "..."}],
 "missing_camps": ["OPPOSE"], "note": "..."}
不要输出解释、不要 Markdown 代码块。"""


@dataclass(frozen=True, slots=True)
class _Candidate:
    id: UUID
    title: str
    angle: str
    audience: str
    thesis: str
    stance_ids: tuple[UUID, ...]
    #: 原样带上，写回时合并而不是覆盖 —— 打分器可能已经写进 score_reason。
    brief: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    stances: list[OpinionStance]
    candidates: list[_Candidate]
    coverage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Decision:
    verdict: str
    reason: str
    audience_identified: bool
    overlap_with: list[int]


@handler("check_opinion")
async def check_opinion(task: TaskContext) -> dict[str, Any]:
    raw = task.input.get("seed_task_id")
    if not raw:
        raise NonRetryableError("check_opinion 需要 input.seed_task_id", code="MISSING_SEED_TASK")
    seed_task_id = UUID(str(raw))

    snapshot = await _load(task.workspace_id, seed_task_id)
    if not snapshot.stances:
        raise NonRetryableError("这一批候选没有立场池，无法做舆论检测", code="NO_STANCE_POOL")
    if not snapshot.candidates:
        # 没有候选不是失败：展开阶段可能把所有候选都丢了。
        return {"checked": 0, "kept": 0, "rejected": [], "note": "没有可检测的候选"}

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=f"舆论检测：{len(snapshot.candidates)} 个候选 / {len(snapshot.stances)} 条立场",
            task_id=task.id,
            attempt=task.attempt,
            max_cost=_max_cost(task),
            max_steps=3,
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        verdicts = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.SELF_CHECK,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(snapshot),
            schema=schemas.OpinionVerdicts,
            shape=schemas.OPINION_VERDICTS_SHAPE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        decisions = _decide(snapshot, verdicts)
        rejected_ids, kept_ids = await _apply(snapshot, decisions, run_id)

        await runtime.finish_run(
            run_id,
            output={
                "checked": len(decisions),
                "rejected": [str(item) for item in rejected_ids],
                "kept": len(kept_ids),
                "coverage": snapshot.coverage,
            },
        )
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    score_task_id = None
    if kept_ids:
        score_task_id = await _enqueue_scoring(task, seed_task_id)
    else:
        # 全部被拒：链条停在这里，而不是把空列表丢给打分器让它失败。
        log.warning(
            "agent.opinion_all_rejected",
            task_id=str(task.id),
            seed_task=str(seed_task_id),
        )

    log.info(
        "agent.opinion_checked",
        task_id=str(task.id),
        checked=len(decisions),
        rejected=len(rejected_ids),
        kept=len(kept_ids),
        score_task=str(score_task_id) if score_task_id else None,
    )
    return {
        "checked": len(decisions),
        "kept": len(kept_ids),
        "rejected": [str(item) for item in rejected_ids],
        "run_id": str(run_id),
        "coverage": snapshot.coverage,
        "score_task_id": str(score_task_id) if score_task_id else None,
    }


# ------------------------------------------------------------------ 判定


def _decide(snapshot: _Snapshot, verdicts: schemas.OpinionVerdicts) -> list[_Decision]:
    """把模型判定与机检合并成最终结论。

    机检优先：立场集合与受众完全相同 → 无论模型怎么说都只能是 WEAK。
    「换皮」是结构事实，不该由一个可能被措辞骗过的模型来裁决。
    """
    by_index = {item.candidate_index: item for item in verdicts.verdicts}
    duplicates = _duplicate_groups(snapshot.candidates)

    decisions: list[_Decision] = []
    for position, candidate in enumerate(snapshot.candidates, start=1):
        item = by_index.get(position)
        if item is None:
            decisions.append(
                _Decision(
                    verdict="WEAK",
                    reason="检测没有对这个候选给出结论",
                    audience_identified=bool(candidate.audience.strip()),
                    overlap_with=[],
                )
            )
            continue

        verdict = item.verdict
        reason = item.reason
        audience_ok = item.audience_identified

        if position in duplicates:
            if verdict == "GROUNDED":
                verdict = "WEAK"
            twin = duplicates[position]
            reason = f"{reason}（与候选 {twin} 的立场集合和受众完全相同）"

        # 受众识别不出来，就不能算「清楚写给谁」。降级而不是直接否掉：
        # 立场是真的，只是还没找准人 —— 这种候选值得留给人看。
        if verdict == "GROUNDED" and not audience_ok:
            verdict = "WEAK"
            reason = f"{reason}（目标受众无法对应到立场池里的具体人群）"

        decisions.append(
            _Decision(
                verdict=verdict,
                reason=reason,
                audience_identified=audience_ok,
                overlap_with=list(item.overlap_with),
            )
        )
    return decisions


def _duplicate_groups(candidates: list[_Candidate]) -> dict[int, int]:
    """找出「立场集合 + 受众」完全相同的候选，返回 {重复者: 第一个出现的序号}。"""
    seen: dict[tuple[frozenset[UUID], str], int] = {}
    duplicates: dict[int, int] = {}
    for position, candidate in enumerate(candidates, start=1):
        key = (frozenset(candidate.stance_ids), candidate.audience.strip())
        first = seen.get(key)
        if first is not None:
            duplicates[position] = first
            continue
        seen[key] = position
    return duplicates


async def _apply(
    snapshot: _Snapshot,
    decisions: list[_Decision],
    run_id: UUID,
) -> tuple[list[UUID], list[UUID]]:
    """写回结论：不合格的置 REJECTED，其余保留原状态。

    状态只在这里改一次。``brief.opinion`` 会同时写进覆盖率，
    这样「当时舆论池里有什么」和「这个候选被怎么判」能一起回看。
    """
    checked_at = datetime.now(UTC).isoformat()
    rejected: list[UUID] = []
    kept: list[UUID] = []
    rejected_rows: list[dict[str, Any]] = []
    kept_rows: list[dict[str, Any]] = []

    for candidate, decision in zip(snapshot.candidates, decisions, strict=True):
        brief = dict(candidate.brief)
        brief["opinion"] = {
            "checked": True,
            "verdict": decision.verdict,
            "reason": decision.reason,
            "audience_identified": decision.audience_identified,
            "overlap_with": decision.overlap_with,
            "coverage": snapshot.coverage,
            "checked_at": checked_at,
            "run_id": str(run_id),
        }
        if decision.verdict == "UNGROUNDED":
            rejected.append(candidate.id)
            rejected_rows.append(
                {"id": candidate.id, "brief": brief, "status": TopicStatus.REJECTED}
            )
        else:
            kept.append(candidate.id)
            kept_rows.append({"id": candidate.id, "brief": brief})

    async with WorkerSession() as session:
        if kept_rows:
            await session.execute(
                update(Topic).execution_options(synchronize_session=False), kept_rows
            )
        if rejected_rows:
            await session.execute(
                update(Topic).execution_options(synchronize_session=False), rejected_rows
            )
        await session.commit()

    return rejected, kept


# ------------------------------------------------------------------ 取数


async def _load(workspace_id: UUID, seed_task_id: UUID) -> _Snapshot:
    async with WorkerSession() as session:
        stances = list(
            (
                await session.execute(
                    select(OpinionStance)
                    .where(
                        OpinionStance.workspace_id == workspace_id,
                        OpinionStance.seed_task_id == seed_task_id,
                    )
                    .order_by(OpinionStance.created_at, OpinionStance.id)
                )
            ).scalars()
        )
        topics = list(
            (
                await session.execute(
                    select(Topic)
                    .where(
                        Topic.workspace_id == workspace_id,
                        Topic.seed_task_id == seed_task_id,
                        Topic.status.in_((TopicStatus.CANDIDATE, TopicStatus.RECOMMENDED)),
                    )
                    .order_by(Topic.created_at, Topic.id)
                    .limit(MAX_CANDIDATES)
                )
            ).scalars()
        )

        links: dict[UUID, list[UUID]] = {}
        if topics:
            rows = (
                await session.execute(
                    select(TopicStance.topic_id, TopicStance.stance_id).where(
                        TopicStance.topic_id.in_([item.id for item in topics])
                    )
                )
            ).all()
            for topic_id, stance_id in rows:
                links.setdefault(topic_id, []).append(stance_id)

        candidates = [
            _Candidate(
                id=item.id,
                title=item.title,
                angle=str((item.brief or {}).get("angle") or ""),
                audience=str((item.brief or {}).get("audience") or ""),
                thesis=str((item.brief or {}).get("thesis") or ""),
                stance_ids=tuple(links.get(item.id, ())),
                brief=dict(item.brief or {}),
            )
            for item in topics
        ]

        event_ids = {item.event_id for item in stances}
        event_rows = (
            (
                await session.execute(
                    select(ExternalEvent.url, ExternalEvent.source_kind).where(
                        ExternalEvent.id.in_(event_ids),
                        ExternalEvent.workspace_id == workspace_id,
                    )
                )
            ).all()
            if event_ids
            else []
        )

    # 来源构成按**材料**算，不按立场算：一篇材料抽三条立场，它仍然只代表
    # 一个人的一次发言。用 URL 现算而不是读列，是为了让改分类表立刻生效
    # （存量数据的 source_kind 是当时判的，规则改了它不会自动跟着变）。
    kind_counts: Counter[str] = Counter()
    for url, stored_kind in event_rows:
        kind, _rule = sources.classify(url) if url else (stored_kind or "UNKNOWN", "stored")
        kind_counts[kind] += 1
    doc_total = sum(kind_counts.values())

    camp_counts = Counter(item.camp for item in stances)
    coverage = {
        "doc_count": len(event_ids),
        "source_count": len({url for url, _ in event_rows if url}),
        "stance_count": len(stances),
        "camp_counts": {camp: camp_counts[camp] for camp in CAMP_ORDER if camp_counts[camp]},
        "missing_camps": [camp for camp in CAMP_ORDER if not camp_counts[camp]],
        # 「谁在说话」的构成。全是 MEDIA 时，camp_counts 只是「几篇文章站哪边」，
        # 不是「多少人站哪边」—— 这个区别以前在数据里根本看不出来。
        "source_mix": {kind: kind_counts[kind] for kind in SOURCE_KINDS if kind_counts[kind]},
        "ugc_ratio": round(kind_counts["UGC"] / doc_total, 2) if doc_total else 0.0,
    }
    return _Snapshot(stances=stances, candidates=candidates, coverage=coverage)


def _build_prompt(snapshot: _Snapshot) -> str:
    lines = [f"立场池（{len(snapshot.stances)} 条）："]
    for index, stance in enumerate(snapshot.stances, start=1):
        lines.append(
            f"[{index}] {stance.label}（{CAMP_LABEL.get(stance.camp, stance.camp)}）\n"
            f"    主张：{stance.claim}\n"
            f"    持有者：{stance.audience}"
        )

    lines.append("")
    lines.append(f"候选（{len(snapshot.candidates)} 个）：")
    label_of = {item.id: item.label for item in snapshot.stances}
    for index, candidate in enumerate(snapshot.candidates, start=1):
        cited = [
            label_of[sid]
            for sid in candidate.stance_ids[:MAX_STANCES_PER_CANDIDATE]
            if sid in label_of
        ]
        lines.append(
            f"[{index}] {candidate.title}\n"
            f"    角度：{candidate.angle}\n"
            f"    目标受众：{candidate.audience}\n"
            f"    论点：{candidate.thesis}\n"
            f"    引用立场：{'、'.join(cited) if cited else '（无）'}"
        )

    lines.append("")
    camp_text = "、".join(
        f"{CAMP_LABEL[camp]} {count}" for camp, count in snapshot.coverage["camp_counts"].items()
    )
    lines.append(
        f"覆盖统计：材料 {snapshot.coverage['doc_count']} 篇 / 来源 "
        f"{snapshot.coverage['source_count']} 个 / 阵营分布：{camp_text or '无'}"
    )
    mix = snapshot.coverage.get("source_mix") or {}
    if mix:
        mix_text = "、".join(
            f"{SOURCE_KIND_LABEL.get(kind, kind)} {count}" for kind, count in mix.items()
        )
        lines.append(f"来源构成：{mix_text}")
    missing = snapshot.coverage["missing_camps"]
    if missing:
        lines.append("立场池里缺失的阵营：" + "、".join(CAMP_LABEL[c] for c in missing))
    return "\n".join(lines)


async def _enqueue_scoring(task: TaskContext, seed_task_id: UUID) -> UUID | None:
    async with WorkerSession() as session:
        created = await create_task(
            session,
            workspace_id=task.workspace_id,
            payload=TaskCreate(
                task_type="score_topics",
                input={"seed_task_id": str(seed_task_id)},
                assigned_agent="scorer",
                dedup_key=f"{seed_task_id}:score",
            ),
        )
    return created.id


def _max_cost(task: TaskContext) -> Decimal:
    raw = task.input.get("max_cost")
    return Decimal(str(raw)) if raw is not None else Decimal("0.3")


__all__ = ["AGENT_NAME", "check_opinion"]
