"""选题管线：一个种子 → 多个候选 → 排序推荐。

验证四件工程事实（不是「选题好不好」）：

1. 展开产出的候选都挂在 ``seed_task_id`` 上 —— 这是溯源与幂等的唯一判据；
2. 重跑展开不会长出第二批候选（租约丢失重跑是常态路径）；
3. 打分只把前三个标 ``RECOMMENDED``，其余留在 ``CANDIDATE`` —— 两者都在
   ``SELECTABLE`` 里，人都能选，推荐只是排序不是闸门；
4. 方向型种子没有事件，``trend`` 必须为空而不是编一个数。

stub provider 的返回是确定性的 3 个候选、且所有 index 都是 1，所以
「六个候选排出前三」这一条用 ``_apply`` 纯函数测，不依赖模型行为。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.agents import expander, opinion, schemas, scorer
from xing.core.config import settings
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.db.worker import dispose as dispose_worker
from xing.models.event import ExternalEvent, SourceType
from xing.models.task import Task, TaskStatus
from xing.models.topic import OpinionStance, Topic, TopicStance, TopicStatus
from xing.models.trace import RetrievalTrace
from xing.search import SearchConfigError, SearchDoc, SearchHit, SearchOptions
from xing.worker import runner
from xing.worker.context import TaskContext


@pytest.fixture(autouse=True)
async def _dispose_worker_engine():
    yield
    await dispose_worker()


@pytest.fixture
async def workspace():
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "insert into users (email, display_name) "
                    "values (:email, 'topic-test') returning id"
                ),
                {"email": f"topic-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('topic-test', :uid) returning id"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "insert into workspace_members (workspace_id, user_id, role) "
                "values (:ws, :uid, 'OWNER')"
            ),
            {"ws": ws_id, "uid": user_id},
        )

    yield ws_id, user_id

    # 队列是全租户共享的（``claim_one`` 不按 workspace 过滤），残留一条 READY
    # 任务就会被下一个测试的 ``_drain`` 领走，症状是「单跑过、全量挂」。
    async with admin.begin() as conn:
        await conn.execute(text("delete from workspaces where id = :ws"), {"ws": ws_id})
    await admin.dispose()

    async with admin.begin() as conn:
        await conn.execute(text("delete from workspaces where id = :id"), {"id": ws_id})
    await admin.dispose()


async def _add_task(ws: UUID, task_type: str, payload: dict[str, Any]) -> UUID:
    async with WorkerSession() as session:
        task = Task(workspace_id=ws, task_type=task_type, input=payload)
        session.add(task)
        await session.commit()
        return task.id


async def _add_event(ws: UUID, title: str) -> UUID:
    async with WorkerSession() as session:
        event = ExternalEvent(
            workspace_id=ws,
            source_type=SourceType.MANUAL,
            external_id=f"manual-{uuid4()}",
            title=title,
            summary="测试用事件摘要",
        )
        session.add(event)
        await session.commit()
        return event.id


async def _task(task_id: UUID) -> Task:
    async with WorkerSession() as session:
        return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _all_tasks(ws: UUID) -> list[Task]:
    async with WorkerSession() as session:
        return list((await session.execute(select(Task).where(Task.workspace_id == ws))).scalars())


async def _topics(ws: UUID) -> list[Topic]:
    async with WorkerSession() as session:
        stmt = select(Topic).where(Topic.workspace_id == ws).order_by(Topic.created_at, Topic.id)
        return list((await session.execute(stmt)).scalars())


async def _drain(ws: UUID, limit: int = 12) -> None:
    """把**本 workspace** 的队列跑空。

    必须带 ``workspace_id``：``claim_one`` 默认扫全租户队列，前一个测试文件
    留下的 READY 任务会被这里领走，占掉配额还不属于本测试 —— 症状是
    「单文件跑过、全量跑挂」。
    """
    for _ in range(limit):
        outcome = await runner.claim_and_run(worker_id="w-topic", workspace_id=ws)
        if outcome is None:
            return
        assert outcome.status in {"SUCCEEDED", "SKIPPED", "FAILED"}, outcome


async def test_direction_seed_expands_then_scores(workspace):
    """方向型种子：展开 → 自动排打分 → 候选带分数，trend 留空。"""
    ws, _ = workspace
    task_id = await _add_task(
        ws,
        "expand_topics",
        {"seed_type": "direction", "direction": "AI 在中型企业落地的真实阻力"},
    )

    await _drain(ws)

    expand = await _task(task_id)
    assert expand.status == TaskStatus.SUCCEEDED, expand.error
    assert expand.output is not None
    topic_ids = expand.output["topic_ids"]
    assert len(topic_ids) >= 3

    topics = await _topics(ws)
    assert len(topics) == len(topic_ids)
    # 溯源：每个候选都指回这次展开
    assert all(t.seed_task_id == task_id for t in topics)
    # 方向型没有事件
    assert all(t.seed_event_id is None for t in topics)
    # expand 写的结构化产出必须在
    assert all(t.brief and t.brief.get("stage") == "EXPANDED" for t in topics)
    assert all(t.brief.get("angle") for t in topics)

    # 打分任务被自动排进队列，且幂等键挂在展开任务上
    score_tasks = [t for t in await _all_tasks(ws) if t.task_type == "score_topics"]
    assert len(score_tasks) == 1
    assert score_tasks[0].status == TaskStatus.SUCCEEDED, score_tasks[0].error
    assert score_tasks[0].dedup_key == f"{task_id}:score"

    # 没有种子事件就没有趋势可言 —— 空着比编一个数诚实
    assert all(t.trend_score is None for t in topics)
    # 至少有一个候选被打了分（stub 只回一个 index，其余保持未打分）
    assert any(t.relevance_score is not None for t in topics)


async def test_event_seed_links_the_event(workspace):
    """事件型种子：候选要能反查到具体事件，且多个候选共享同一个 seed_event_id。"""
    ws, _ = workspace
    event_id = await _add_event(ws, "某银行 AI 审批误判导致大面积投诉")

    task_id = await _add_task(
        ws,
        "expand_topics",
        {"seed_type": "event", "event_id": str(event_id), "count": 4},
    )
    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.SUCCEEDED, task.error

    topics = await _topics(ws)
    assert len(topics) >= 3
    assert all(t.seed_event_id == event_id for t in topics)
    # primary_event_id 留给「深入之后确定的主证据」，展开阶段不能占它 ——
    # 那个字段上有唯一索引，占了就变成一个事件只能一个选题。
    assert all(t.primary_event_id is None for t in topics)


async def test_expand_rerun_does_not_create_second_batch(workspace):
    """租约丢失重跑：候选数量不变，返回 reused。"""
    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "重复展开测试"}
    )
    await _drain(ws)
    before = len(await _topics(ws))
    assert before >= 3

    task = await _task(task_id)
    out = await expander.expand_topics(TaskContext.from_task(task))

    assert out["reused"] is True
    assert len(await _topics(ws)) == before


async def test_missing_event_id_fails_without_retry(workspace):
    """参数缺失是永久错误：直接 FAILED，不该反复重试烧钱。"""
    ws, _ = workspace
    task_id = await _add_task(ws, "expand_topics", {"seed_type": "event"})

    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "MISSING_EVENT_ID"


async def test_unknown_event_is_rejected(workspace):
    """不存在的 event_id：不能静默当成空种子继续跑。"""
    ws, _ = workspace
    task_id = await _add_task(ws, "expand_topics", {"seed_type": "event", "event_id": str(uuid4())})

    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "EVENT_NOT_FOUND"


async def test_ranking_marks_only_top_three(workspace):
    """六个候选排前三。用 _apply 直接注入评分，不依赖模型行为。"""
    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "排序测试"}
    )

    async with WorkerSession() as session:
        for i in range(6):
            session.add(
                Topic(
                    workspace_id=ws,
                    seed_task_id=task_id,
                    title=f"候选{i + 1}",
                    status=TopicStatus.CANDIDATE,
                    brief={"stage": "EXPANDED", "angle": f"角度{i + 1}"},
                )
            )
        await session.commit()

    candidates = await scorer._load(ws, task_id)
    assert len(candidates) == 6

    verdicts = schemas.TopicScores(
        scores=[
            schemas.TopicScore(index=i, relevance=0.1 * i, potential=0.1 * i, reason=f"理由{i}")
            for i in range(1, 7)
        ]
    )
    ranked = await scorer._apply(candidates, verdicts, None)

    overalls = [item["overall"] for item in ranked]
    assert overalls == sorted(overalls, reverse=True)
    assert [item["status"] for item in ranked].count(TopicStatus.RECOMMENDED) == 3

    rows = await _topics(ws)
    assert sum(1 for t in rows if t.status == TopicStatus.RECOMMENDED) == 3
    assert sum(1 for t in rows if t.status == TopicStatus.CANDIDATE) == 3
    # 分数留痕，理由并进 brief 而不是覆盖 expand 写的 angle
    assert all(t.brief and t.brief.get("score_reason") for t in rows)
    assert all(t.brief and t.brief.get("angle") for t in rows)


async def test_all_indexes_out_of_range_is_terminal(workspace):
    """模型给了一堆不存在的序号：这是输出不可用，不是「这次没啥好评的」。"""
    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "越界测试"}
    )
    async with WorkerSession() as session:
        session.add(
            Topic(
                workspace_id=ws,
                seed_task_id=task_id,
                title="唯一候选",
                status=TopicStatus.CANDIDATE,
                brief={"stage": "EXPANDED"},
            )
        )
        await session.commit()

    candidates = await scorer._load(ws, task_id)
    # index 本身合法（契约限制 1..8），但只有 1 个候选 —— 序号对不上任何一行。
    verdicts = schemas.TopicScores(
        scores=[schemas.TopicScore(index=3, relevance=0.5, potential=0.5, reason="越界")]
    )

    with pytest.raises(NonRetryableError):
        await scorer._apply(candidates, verdicts, None)


async def test_expand_anchors_why_now_with_one_search(workspace):
    """展开会做一次背景检索：why_now 有外部依据，不再是模型凭常识给的。"""
    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "AI 落地的真实阻力"}
    )

    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.SUCCEEDED, task.error
    assert task.output is not None
    # 检索拿到了材料，并且这个数字进了 output —— 事后能回答「当时看到了什么」
    assert task.output["seed_docs"] >= 1

    topics = await _topics(ws)
    assert all(t.brief and t.brief.get("seed_grounded") is True for t in topics)


async def test_empty_search_is_terminal(workspace, monkeypatch):
    """搜不到材料就没有立场池 —— 不能退回模型先验，必须终态。"""

    class _EmptySearcher:
        name = "tavily"
        concurrent_safe = False

        async def search(
            self, query: str, *, max_results: int = 4, options: SearchOptions | None = None
        ) -> SearchHit:
            return SearchHit(query=query, docs=(), provider=self.name)

    monkeypatch.setattr(expander, "build_searcher", lambda **_: _EmptySearcher())

    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "搜不到材料的方向"}
    )
    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "NO_OPINION_SOURCE"
    # 一条候选都不该被写进去
    assert await _topics(ws) == []


async def test_thin_opinion_is_terminal_for_real_searchers(workspace, monkeypatch):
    """真实检索下，只有一种声音的题直接失败 —— 它没有分歧可写。

    stub provider 的立场恒为三条同标签，所以这条测试同时验证了
    「多样性闸门对非 stub 检索器生效，对 stub 跳过」。
    """

    class _OneVoiceSearcher:
        name = "tavily"
        concurrent_safe = False

        async def search(
            self, query: str, *, max_results: int = 4, options: SearchOptions | None = None
        ) -> SearchHit:
            doc = SearchDoc(
                title="同一种声音",
                snippet="stub 是唯一被反复引用的一句原文。",
                url="https://example.com/one",
                source_label="example.com",
            )
            return SearchHit(query=query, docs=(doc,), provider=self.name)

    monkeypatch.setattr(expander, "build_searcher", lambda **_: _OneVoiceSearcher())

    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "只有一种声音的题"}
    )
    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "OPINION_TOO_THIN"
    # 补采轮跑过（同一批 URL 不重复喂，所以补不到新声音），错误里要说清楚，
    # 否则事后无法区分「没补」和「补了没有」。
    assert "补采" in task.error["message"]


async def test_search_misconfiguration_is_terminal(workspace, monkeypatch):
    """配置错重试三次也不会变对 —— 必须终态，而不是烧完预算。"""

    def boom(**_: Any) -> Any:
        raise SearchConfigError("search_provider=qwen 需要 llm_provider=openai")

    monkeypatch.setattr(expander, "build_searcher", boom)

    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "配置错误"}
    )
    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "SEARCH_NOT_CONFIGURED"


async def test_expand_writes_stance_pool_and_links(workspace):
    """展开的产物不只是候选，还有立场池与「候选 → 立场」的引用。

    stub 环境里三条立场同标签、被唯一索引吸收成一行，三个候选引用的也是
    同一组立场 —— 于是舆论检测的机检会把它们全部降为 WEAK。这恰好证明了
    「换皮」判定不依赖模型：结构相同就是同一条。
    """
    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "立场池落库"}
    )
    await _drain(ws)

    task = await _task(task_id)
    assert task.status == TaskStatus.SUCCEEDED, task.error
    assert task.output is not None
    assert task.output["stance_count"] >= 1

    async with WorkerSession() as session:
        stances = list(
            (
                await session.execute(
                    select(OpinionStance).where(OpinionStance.seed_task_id == task_id)
                )
            ).scalars()
        )
        links = list(
            (
                await session.execute(select(TopicStance).where(TopicStance.workspace_id == ws))
            ).scalars()
        )
        topics = await _topics(ws)

    assert stances, "立场池没有落库"
    # 每一条立场都必须能回查到材料原文
    assert all(stance.quote and stance.event_id for stance in stances)
    # 每个候选至少引用一条立场：没有引用的候选不该存在
    assert len({link.topic_id for link in links}) == len(topics)
    assert all(topic.brief and topic.brief.get("stances") for topic in topics)

    # 舆论检测已经跑过，结论写回 brief。三个候选立场与受众完全相同，
    # 机检保留第一个、把后两个降为 WEAK —— 不靠模型也能识别换皮。
    verdicts = [(topic.brief or {})["opinion"]["verdict"] for topic in topics]
    assert verdicts.count("GROUNDED") == 1
    assert verdicts.count("WEAK") == len(topics) - 1

    # 检测通过后才排打分，链顺序是 expand → opinion → score
    types = [t.task_type for t in await _all_tasks(ws)]
    assert types.index("check_opinion") < types.index("score_topics")


def test_candidate_without_stance_is_dropped():
    """引用为空或全部越界的候选直接丢掉：这是「禁止模型先验」的落地点。"""
    stances = [
        expander.Stance(
            event_id=uuid4(),
            label="责任在签字人",
            camp="OPPOSE",
            claim="模型不该背这个锅",
            audience="一线审批员",
            quote="原文",
        )
    ]
    result = schemas.GroundedCandidates(
        candidates=[
            schemas.GroundedCandidate(
                title="有引用",
                angle="从签字责任切",
                audience="合规负责人",
                thesis="签字权才是瓶颈",
                why_now="仍在争论",
                stance_indexes=[1],
            ),
            schemas.GroundedCandidate(
                title="引用越界",
                angle="凭空的角度",
                audience="大众",
                thesis="AI 很重要",
                why_now="最近",
                stance_indexes=[9],
            ),
            schemas.GroundedCandidate(
                title="另一个有引用",
                angle="从代价切",
                audience="一线审批员",
                thesis="不签字是因为没人兜底",
                why_now="仍在争论",
                stance_indexes=[1],
            ),
        ]
    )

    kept, dropped = expander._ground(result, stances)

    assert dropped == 1
    assert len(kept) == 2
    assert kept[0].candidate.title == "有引用"
    assert kept[0].indexes == (1,)


def test_stance_quote_must_be_a_substring_of_the_material():
    """立场引文机检：改写过的句子不能算「有人这么说」。"""
    stored = [
        expander._Doc(
            index=1,
            doc=SearchDoc(
                title="审批责任争议",
                snippet="有合规负责人说：模型没有法人资格，签字的人就该承担后果。",
            ),
            event_id=uuid4(),
        )
    ]
    raw = schemas.StanceSet(
        stances=[
            schemas.StanceItem(
                doc_index=1,
                label="责任在签字人",
                camp="OPPOSE",
                claim="模型不能承担责任",
                audience="合规负责人",
                quote="模型没有法人资格，签字的人就该承担后果",
            ),
            schemas.StanceItem(
                doc_index=1,
                label="责任在厂商",
                camp="SUPPORT",
                claim="应该由供应商兜底",
                audience="采购负责人",
                quote="出了问题应该由厂商赔付",  # 原文里没有这句
            ),
            schemas.StanceItem(
                doc_index=1,
                label="责任说不清",
                camp="OBSERVER",
                claim="责任边界本身不清",
                audience="观察者",
                quote="模型没有法人资格",
            ),
        ]
    )

    accepted, rejected = expander._validate_stances(raw, stored)

    assert rejected == 1
    assert len(accepted) == 2
    assert accepted[0].label == "责任在签字人"
    assert accepted[0].event_id == stored[0].event_id
    assert all("出了问题" not in item.quote for item in accepted)


def test_opinion_check_downgrades_duplicates_and_rejects_ungrounded():
    """机检优先于模型：立场与受众完全相同 = 换皮；没有舆论支撑 = 拒。"""
    stance_a, stance_b = uuid4(), uuid4()

    def candidate(uid, audience, stance_id):
        return opinion._Candidate(
            id=uid,
            title=f"候选{uid}",
            angle="角度",
            audience=audience,
            thesis="论点",
            stance_ids=(stance_id,),
            brief={"stage": "EXPANDED"},
        )

    snapshot = opinion._Snapshot(
        stances=[],
        candidates=[
            candidate(uuid4(), "合规负责人", stance_a),
            candidate(uuid4(), "合规负责人", stance_a),  # 与 1 完全同构
            candidate(uuid4(), "一线审批员", stance_b),
        ],
        coverage={"doc_count": 2, "source_count": 2, "stance_count": 2},
    )
    verdicts = schemas.OpinionVerdicts(
        verdicts=[
            schemas.OpinionVerdictItem(
                candidate_index=1,
                verdict="GROUNDED",
                audience_identified=True,
                overlap_with=[],
                reason="有明确持有者",
            ),
            schemas.OpinionVerdictItem(
                candidate_index=2,
                verdict="GROUNDED",
                audience_identified=True,
                overlap_with=[],
                reason="有明确持有者",
            ),
            schemas.OpinionVerdictItem(
                candidate_index=3,
                verdict="UNGROUNDED",
                audience_identified=False,
                overlap_with=[],
                reason="立场池里没有这个切面",
            ),
        ]
    )

    decisions = opinion._decide(snapshot, verdicts)

    assert decisions[0].verdict == "GROUNDED"
    # 模型说 GROUNDED，但机检发现与 1 同构 → WEAK
    assert decisions[1].verdict == "WEAK"
    assert "完全相同" in decisions[1].reason
    assert decisions[2].verdict == "UNGROUNDED"


# ---------------------------------------------------------------- 补采缺失阵营


def test_missing_camps_and_merge_dedupe():
    """缺口是集合差，合并按标签去重 —— 两个纯函数，不依赖模型。"""

    def stance(label: str, camp: str) -> expander.Stance:
        return expander.Stance(
            event_id=uuid4(),
            label=label,
            camp=camp,
            claim="主张",
            audience="某群人",
            quote="原文",
        )

    base = [stance("这是形式主义", "OPPOSE"), stance("亲历者说没用", "EXPERIENCE")]

    assert expander._missing_camps(base) == ["SUPPORT", "CONDITIONAL", "OBSERVER"]
    # 补采只看「构成一边声音」的四个阵营，OBSERVER 缺了不为它多跑一轮
    assert expander._missing_camps(base, for_supplement=True) == ["SUPPORT", "CONDITIONAL"]

    # 同一句话被另一家媒体转述：不该算第二条立场
    merged, duplicates = expander._merge_stances(
        base,
        [stance("  这是形式主义 ", "OPPOSE"), stance("支持但要设前提", "CONDITIONAL")],
    )
    assert duplicates == 1
    assert [item.label for item in merged] == [
        "这是形式主义",
        "亲历者说没用",
        "支持但要设前提",
    ]


def test_seed_core_strips_field_label():
    """补采 query 的核心词不能是「方向」这个词本身。

    ``_resolve_seed`` 把种子拼成「方向：<正文>」，按标点切第一段会把标签留在
    最前面 —— 第一次接上补采时，query 真的成了「方向 确实有用 效果」。
    """

    assert expander._seed_core("方向：公司要求用 AI 写周报") == "公司要求用 AI 写周报"
    # 带冒号的长句只取第一段：第二段是「还是」，拼进 query 会退化成字面匹配
    assert (
        expander._seed_core("方向：公司要求全员用 AI 写周报：效率提升，还是新的形式主义")
        == "公司要求全员用 AI 写周报"
    )
    # 事件型种子是多行字段，只取标题
    assert expander._seed_core("事件：某公司全员 AI 周报\n摘要：员工不满") == "某公司全员 AI 周报"

    queries = expander._supplement_queries("方向：AI 写周报", ["EXPERIENCE"])
    assert queries and all("方向" not in q for q in queries)
    assert all("亲历" in q or "三个月" in q for q in queries)


async def test_supplement_adds_missing_camp_to_pool(workspace, monkeypatch):
    """缺 CONDITIONAL：补采一轮，新立场必须真的进池，并且能被候选引用。

    用脚本化的 ``call_structured`` 而不是 stub provider：stub 的立场恒为
    「SUPPORT/stub」，表达不了「第一轮只有反对、第二轮才补到有条件支持」。
    这里要验的是补采的接线（换一批材料 → 再抽一次 → 合并），不是模型水平。
    """

    class _TwoRoundSearcher:
        name = "tavily"
        concurrent_safe = False

        async def search(
            self, query: str, *, max_results: int = 4, options: SearchOptions | None = None
        ) -> SearchHit:
            # 补采 query 里必然带「前提」（见 SYSTEM_SUPPLEMENT_QUERIES 的措辞要求）
            if "前提" in query:
                doc = SearchDoc(
                    title="有条件支持",
                    snippet="我在用，也确实省时间，但前提是不许拿去考核人。",
                    url="https://example.com/supp",
                    source_label="example.com",
                )
            else:
                doc = SearchDoc(
                    title="反对",
                    snippet="这东西就是新形式主义，写得更快也更空。",
                    url="https://example.com/opp",
                    source_label="example.com",
                )
            return SearchHit(query=query, docs=(doc,), provider=self.name)

    monkeypatch.setattr(expander, "build_searcher", lambda **_: _TwoRoundSearcher())

    responses: list[Any] = [
        schemas.OpinionPlan(
            queries=[
                schemas.OpinionQuery(query="AI 周报 形式主义 反对", target="OPPOSE"),
                schemas.OpinionQuery(query="AI 周报 员工 吐槽", target="OPPOSE"),
                schemas.OpinionQuery(query="AI 周报 一线员工 感受", target="VOICE"),
            ]
        ),
        # 第一轮：三条都是反对，label 各不相同（唯一索引按 label 去重）
        schemas.StanceSet(
            stances=[
                schemas.StanceItem(
                    doc_index=1,
                    label="AI 周报是新形式主义",
                    camp="OPPOSE",
                    claim="写得更快也更空",
                    audience="被强制写周报的员工",
                    quote="这东西就是新形式主义，写得更快也更空",
                ),
                schemas.StanceItem(
                    doc_index=1,
                    label="周报本来就没信息量",
                    camp="OPPOSE",
                    claim="周报是给领导看的表演",
                    audience="中层管理者",
                    quote="这东西就是新形式主义",
                ),
                schemas.StanceItem(
                    doc_index=1,
                    label="强制考核会逼人造假",
                    camp="OPPOSE",
                    claim="一考核就会有人编",
                    audience="HR 负责人",
                    quote="写得更快也更空",
                ),
            ]
        ),
        # 补采轮：1 条引文合法（CONDITIONAL），2 条是改写 → 应被机检丢掉
        schemas.StanceSet(
            stances=[
                schemas.StanceItem(
                    doc_index=1,
                    label="支持但要先划清考核边界",
                    camp="CONDITIONAL",
                    claim="能用，但不能拿来考核人",
                    audience="愿意用 AI 但反对考核的员工",
                    quote="我在用，也确实省时间，但前提是不许拿去考核人",
                ),
                schemas.StanceItem(
                    doc_index=1,
                    label="效率提升可以量化",
                    camp="SUPPORT",
                    claim="能省一半时间",
                    audience="运营负责人",
                    quote="效率提升了百分之五十",  # 材料里没有
                ),
                schemas.StanceItem(
                    doc_index=1,
                    label="厂商会提供报表",
                    camp="SUPPORT",
                    claim="供应商会配套统计",
                    audience="采购",
                    quote="供应商提供完整报表",  # 材料里没有
                ),
            ]
        ),
        schemas.GroundedCandidates(
            candidates=[
                schemas.GroundedCandidate(
                    title="周报的表演性",
                    angle="从形式主义切",
                    audience="被强制写周报的员工",
                    thesis="周报从来不是为了同步信息",
                    why_now="仍在争论",
                    stance_indexes=[1],
                ),
                schemas.GroundedCandidate(
                    title="考核一旦落地就会造假",
                    angle="从考核副作用切",
                    audience="HR 负责人",
                    thesis="考核指标会先于内容腐坏",
                    why_now="仍在争论",
                    stance_indexes=[2],
                ),
                schemas.GroundedCandidate(
                    title="能用，但不该被考核",
                    angle="从有条件支持切",
                    audience="愿意用 AI 的员工",
                    thesis="工具可用与考核是两件事",
                    why_now="仍在争论",
                    stance_indexes=[1, 4],
                ),
            ]
        ),
    ]
    queue = list(responses)

    async def fake_call_structured(run_id: UUID, **_: Any) -> Any:
        return queue.pop(0)

    monkeypatch.setattr(expander.runtime, "call_structured", fake_call_structured)

    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "公司要求用 AI 写周报"}
    )
    out = await expander.expand_topics(TaskContext.from_task(await _task(task_id)))

    # 补采真的发生了，而且是针对缺口
    assert out["supplement"]["rounds"] == 1
    assert out["supplement"]["missing_before"] == ["SUPPORT", "CONDITIONAL", "EXPERIENCE"]
    assert "CONDITIONAL" not in out["supplement"]["missing_after"]
    # 三条 query 命中两份材料，但第一轮那篇在 exclude 里 → 只剩一份新的
    assert out["supplement"]["new_docs"] == 1
    assert out["supplement"]["new_stances"] == 1
    # 补采轮的引文机检同样生效：两条改写被丢
    assert out["supplement"]["dropped_stances"] == 2

    # 立场池：3 条第一轮 + 1 条补采
    assert out["stance_count"] == 4
    assert out["doc_count"] == 2

    async with WorkerSession() as session:
        stances = list(
            (
                await session.execute(
                    select(OpinionStance).where(OpinionStance.seed_task_id == task_id)
                )
            ).scalars()
        )
        assert len(stances) == 4
        assert {item.camp for item in stances} == {"OPPOSE", "CONDITIONAL"}
        conditional = next(item for item in stances if item.camp == "CONDITIONAL")

        topics = await _topics(ws)
        assert len(topics) == 3
        # 第三个候选引用了补采来的立场（序号 4）
        third = next(item for item in topics if item.title == "能用，但不该被考核")
        links = list(
            (
                await session.execute(select(TopicStance).where(TopicStance.topic_id == third.id))
            ).scalars()
        )
        assert {link.stance_id for link in links} == {
            conditional.id,
            next(item.id for item in stances if item.label == "AI 周报是新形式主义"),
        }
        # 主立场是引用顺序里的第一个（序号 1），补采来的那条是支撑
        primary = next(link for link in links if link.is_primary)
        assert primary.stance_id != conditional.id

        # 留痕进 brief，界面/脚本都能看到补了什么
        assert third.brief["sources"]["supplement"]["rounds"] == 1
        assert third.brief["sources"]["supplement"]["new_docs"] == 1
        assert "CONDITIONAL" in third.brief["sources"]["supplement"]["missing_before"]


async def test_supplement_with_no_usable_stance_still_succeeds(workspace, monkeypatch):
    """补采一条立场都没抽到不是失败：缺口照实记录，候选照常出。

    回归：``StanceSet`` 曾有 ``min_length=3``，于是「补采材料里确实没立场」
    会被 Pydantic 判成格式错误 → repair → ``INVALID_OUTPUT``，整条链死在一个
    采样结果上。模型没做错，格式也没错，错的是把数量写进了契约。
    """

    class _TwoRoundSearcher:
        name = "tavily"
        concurrent_safe = False

        async def search(
            self, query: str, *, max_results: int = 4, options: SearchOptions | None = None
        ) -> SearchHit:
            # 讨论层（带 include_domains）回到同一篇媒体材料：这一例要验的是
            # 补采轮「材料里没立场」，不能让讨论层先把新材料带进来。
            discussion = bool(options and options.include_domains)
            fresh = not discussion and "亲历" in query
            doc = SearchDoc(
                title="新材料" if fresh else "反对",
                snippet=(
                    "材料里通篇在讲别的事，没有可用立场。"
                    if fresh
                    else "这东西就是新形式主义，写得更快也更空。"
                ),
                url="https://example.com/b" if fresh else "https://example.com/a",
                source_label="example.com",
            )
            return SearchHit(query=query, docs=(doc,), provider=self.name)

    monkeypatch.setattr(expander, "build_searcher", lambda **_: _TwoRoundSearcher())

    def stance(label: str, camp: str) -> schemas.StanceItem:
        return schemas.StanceItem(
            doc_index=1,
            label=label,
            camp=camp,
            claim="写得更快也更空",
            audience="被强制写周报的员工",
            quote="这东西就是新形式主义，写得更快也更空",
        )

    responses: list[Any] = [
        schemas.OpinionPlan(
            queries=[
                schemas.OpinionQuery(query="AI 周报 形式主义", target="OPPOSE"),
                schemas.OpinionQuery(query="AI 周报 吐槽", target="OPPOSE"),
                schemas.OpinionQuery(query="AI 周报 员工", target="VOICE"),
            ]
        ),
        # 让缺口只剩 EXPERIENCE：补采只会发亲历者那两条 query，
        # 「补了但一条都没抽到」才是这一例的唯一变量
        schemas.StanceSet(
            stances=[
                stance("新形式主义", "OPPOSE"),
                stance("其实就是走过场", "SUPPORT"),
                stance("能用但别拿来考核", "CONDITIONAL"),
            ]
        ),
        # 补采轮：材料讲的是别的事，一条立场都抽不出来 —— 合法结果
        schemas.StanceSet(stances=[]),
        schemas.GroundedCandidates(
            candidates=[
                schemas.GroundedCandidate(
                    title="表演性",
                    angle="从形式主义切",
                    audience="被强制写周报的员工",
                    thesis="周报从不为同步信息",
                    why_now="仍在争论",
                    stance_indexes=[1],
                ),
                schemas.GroundedCandidate(
                    title="没有信息量",
                    angle="从信息密度切",
                    audience="中层管理者",
                    thesis="周报越写越空",
                    why_now="仍在争论",
                    stance_indexes=[2],
                ),
                schemas.GroundedCandidate(
                    title="考核逼人造假",
                    angle="从考核副作用切",
                    audience="HR 负责人",
                    thesis="考核指标先于内容腐坏",
                    why_now="仍在争论",
                    stance_indexes=[3],
                ),
            ]
        ),
    ]
    queue = list(responses)

    async def fake_call_structured(run_id: UUID, **_: Any) -> Any:
        return queue.pop(0)

    monkeypatch.setattr(expander.runtime, "call_structured", fake_call_structured)

    ws, _ = workspace
    task_id = await _add_task(
        ws, "expand_topics", {"seed_type": "direction", "direction": "补采抽不到东西"}
    )
    out = await expander.expand_topics(TaskContext.from_task(await _task(task_id)))

    # 补采跑过，但什么都没补到 —— 如实记录，不假装成功也不判死
    assert out["supplement"]["rounds"] == 1
    assert out["supplement"]["new_docs"] == 1
    assert out["supplement"]["new_stances"] == 0
    assert out["supplement"]["missing_after"] == ["EXPERIENCE"]
    # 立场池仍是第一轮那三条，候选照常落库
    assert out["stance_count"] == 3
    assert len(await _topics(ws)) == 3


async def test_search_all_records_layers_options_and_drop_reasons(workspace):
    """一条检索的取证记录必须能解释「搜了几条、为什么只多了两篇」。

    三种丢弃原因对应三种完全不同的处置：上一轮已有（换词重搜）、本轮重复
    （query 之间撞了）、超出上限（调 MAX_DOCS）。混成一个「丢了几条」的数字
    就再也分不清该动哪一处,所以这里逐个断言。
    """

    class _LayeredSearcher:
        name = "tavily"
        concurrent_safe = False

        async def search(
            self, query: str, *, max_results: int = 4, options: SearchOptions | None = None
        ) -> SearchHit:
            url = (
                "https://www.zhihu.com/question/1"
                if options and options.include_domains
                else "https://m.36kr.com/p/1"
            )
            return SearchHit(
                query=query,
                docs=(SearchDoc(title="材料", snippet="一句原文。", url=url),),
                provider=self.name,
                options=options or SearchOptions(),
            )

    ws, _ = workspace
    task_id = await _add_task(ws, "expand_topics", {"seed_type": "direction", "direction": "取证"})
    task = TaskContext.from_task(await _task(task_id))

    retrievals = [
        expander._Retrieval(text="AI 周报 形式主义", layer="MEDIA", target="OPPOSE"),
        expander._Retrieval(text="AI 周报 吐槽", layer="MEDIA", target="OPPOSE"),
        expander._Retrieval(
            text="AI 周报 怎么看",
            layer="DISCUSSION",
            target="VOICE",
            options=SearchOptions(include_domains=("zhihu.com",), time_range="year"),
        ),
    ]
    docs, traces = await expander._search_all(_LayeredSearcher(), retrievals)
    await expander._record_traces(task, uuid4(), round_no=0, traces=traces)

    assert len(docs) == 2, "同一篇材料命中两条 query，只该进池一次"

    async with WorkerSession() as session:
        rows = list(
            (
                await session.execute(
                    select(RetrievalTrace)
                    .where(RetrievalTrace.task_id == task_id)
                    .order_by(RetrievalTrace.seq)
                )
            )
            .scalars()
            .all()
        )

    assert [row.layer for row in rows] == ["MEDIA", "MEDIA", "DISCUSSION"]

    first, second, third = rows
    assert first.status == "OK"
    assert first.docs[0]["source_kind"] == "MEDIA"
    assert first.docs[0]["kind_rule"] == "domain:36kr.com"
    assert first.docs[0]["kept"] is True

    # 第二条 query 命中同一篇：留下的是「重复」，不是「搜不到」。
    assert second.new_count == 0
    assert second.docs[0]["drop_reason"] == "DUPLICATE"

    # 讨论层：参数真的下发了，材料被归成 UGC —— 这是「有人自己在说话」的唯一证据。
    assert third.docs[0]["source_kind"] == "UGC"
    assert third.options["include_domains"] == ["zhihu.com"]
    assert third.options["time_range"] == "year"
    assert third.new_count == 1

    # 重跑同一次尝试不会让轨迹翻倍（ON CONFLICT DO NOTHING 打在唯一键上）
    await expander._record_traces(task, uuid4(), round_no=0, traces=traces)
    async with WorkerSession() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(RetrievalTrace)
                .where(RetrievalTrace.task_id == task_id)
            )
        ).scalar_one()
    assert count == 3
