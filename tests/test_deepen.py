"""深入环节：检索 → 证据 → brief → 排产。

验证的都不是「写得好不好」，而是五件工程事实：

1. 深入**真的去检索了**：材料落在 ``external_events(SEARCH)``，不是模型记忆；
2. 证据的 ``quote`` 必须是材料子串 —— 改写过的引文一律丢掉（机检，无模型参与）；
3. ``brief`` 升级到 ``DEEPENED``，同时保留展开写的 angle/audience；
4. 重跑不重复检索、不重复挂证据（租约丢失是常态路径）；
5. 深入跑完，写作与审核已经在队列里 —— 链条自己走到成稿。

stub 检索器返回确定性材料，片段里恒含 "stub"，而 stub provider 合成的
``quote`` 恒为 "stub"，所以第 2 条在离线环境里也能被真实执行到。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from xing.agents import deepener, schemas, writer
from xing.core.config import settings
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.db.worker import dispose as dispose_worker
from xing.models.content import Content, ContentVersion
from xing.models.event import ExternalEvent, SourceType
from xing.models.task import Task, TaskStatus
from xing.models.topic import Topic, TopicEvent, TopicStatus
from xing.search import SearchDoc
from xing.services import topic_service
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
                    "values (:email, 'deepen-test') returning id"
                ),
                {"email": f"deepen-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('deepen-test', :uid) returning id"
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

    async with admin.begin() as conn:
        await conn.execute(text("delete from workspaces where id = :id"), {"id": ws_id})
    await admin.dispose()


async def _add_topic(ws: UUID, *, title: str = "AI 审批失误之后，谁签字") -> UUID:
    async with WorkerSession() as session:
        topic = Topic(
            workspace_id=ws,
            title=title,
            status=TopicStatus.SELECTED,
            description="诉讼与合规｜50~500 人企业决策者",
            brief={
                "stage": "EXPANDED",
                "angle": "从责任归属切",
                "audience": "企业决策者",
                "thesis": "卡住的不是模型能力，是没人愿意签字",
                "why_now": "近期多起 AI 审批纠纷进入诉讼",
            },
        )
        session.add(topic)
        await session.commit()
        return topic.id


async def _add_task(ws: UUID, task_type: str, payload: dict[str, Any]) -> UUID:
    async with WorkerSession() as session:
        task = Task(workspace_id=ws, task_type=task_type, input=payload)
        session.add(task)
        await session.commit()
        return task.id


async def _task(task_id: UUID) -> Task:
    async with WorkerSession() as session:
        return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _topics(ws: UUID) -> list[Topic]:
    async with WorkerSession() as session:
        return list(
            (await session.execute(select(Topic).where(Topic.workspace_id == ws))).scalars()
        )


async def _drain(limit: int = 12) -> None:
    for _ in range(limit):
        outcome = await runner.claim_and_run(worker_id="w-deepen")
        if outcome is None:
            return
        assert outcome.status in {"SUCCEEDED", "SKIPPED", "FAILED"}, outcome


async def test_deepen_grounds_brief_in_retrieved_evidence(workspace):
    """一次深入：检索落库、证据挂上、brief 升级、写作与审核自动排产。"""
    ws, _ = workspace
    topic_id = await _add_topic(ws)
    task_id = await _add_task(ws, "deepen_topic", {"topic_id": str(topic_id)})

    await _drain()

    task = await _task(task_id)
    assert task.status == TaskStatus.SUCCEEDED, task.error

    topic = (await _topics(ws))[0]
    brief = topic.brief or {}
    assert brief["stage"] == "DEEPENED"
    # 展开阶段的信息不能被深入覆盖 —— 它解释「为什么是这个角度」
    assert brief["angle"] == "从责任归属切"
    assert brief["audience"] == "企业决策者"
    # 深入的产出
    assert brief["thesis"]
    assert len(brief["outline"]) >= 3
    assert brief["evidence_refs"]
    assert brief["search"]["provider"] == "stub"
    # stub 检索必须留下降级标记，否则假证据会冒充真证据
    assert brief["search"]["degraded"] is True
    assert brief["search"]["doc_count"] >= 3

    # 检索材料真的落进了 external_events，而不是只活在 prompt 里
    async with WorkerSession() as session:
        events = list(
            (
                await session.execute(
                    select(ExternalEvent).where(
                        ExternalEvent.workspace_id == ws,
                        ExternalEvent.source_type == SourceType.SEARCH,
                    )
                )
            ).scalars()
        )
        links = list(
            (
                await session.execute(select(TopicEvent).where(TopicEvent.topic_id == topic_id))
            ).scalars()
        )
    assert len(events) >= 3
    assert all(event.external_id for event in events)
    assert links, "证据没有挂到选题上"
    assert all(link.quote for link in links)
    # 主证据指向其中一条材料（唯一索引允许的前提下）
    assert topic.primary_event_id in {link.event_id for link in links}

    # 链条自己走到成稿：写作、审核都已建好且跑完
    async with WorkerSession() as session:
        tasks = list((await session.execute(select(Task).where(Task.workspace_id == ws))).scalars())
        content = (
            await session.execute(select(Content).where(Content.workspace_id == ws))
        ).scalar_one_or_none()

    by_type = {item.task_type: item for item in tasks}
    assert by_type["write_content"].status == TaskStatus.SUCCEEDED
    assert by_type["review_content"].status == TaskStatus.SUCCEEDED
    assert by_type["deepen_topic"].dedup_key is None  # 手工建的任务没有幂等键
    assert by_type["write_content"].dedup_key == f"{topic_id}:write"
    assert by_type["review_content"].dedup_key == f"{topic_id}:review"

    assert content is not None
    async with WorkerSession() as session:
        version = (
            await session.execute(
                select(ContentVersion).where(ContentVersion.content_id == content.id)
            )
        ).scalar_one()
    # 写作时看到的证据要留痕，否则「正文引了 [3]」无法回溯
    evidence_ids = version.extra_metadata.get("evidence_event_ids") or []
    assert len(evidence_ids) == len(links)


async def test_rerun_reuses_deepened_brief(workspace):
    """租约丢失重跑：不再检索、不再挂第二遍证据。"""
    ws, _ = workspace
    topic_id = await _add_topic(ws)
    task_id = await _add_task(ws, "deepen_topic", {"topic_id": str(topic_id)})
    await _drain()

    async with WorkerSession() as session:
        before = len(
            (await session.execute(select(TopicEvent).where(TopicEvent.topic_id == topic_id)))
            .scalars()
            .all()
        )

    task = await _task(task_id)
    out = await deepener.deepen_topic(TaskContext.from_task(task))

    assert out["reused"] is True
    async with WorkerSession() as session:
        after = len(
            (await session.execute(select(TopicEvent).where(TopicEvent.topic_id == topic_id)))
            .scalars()
            .all()
        )
    assert after == before


def test_quote_must_be_a_substring_of_the_material():
    """引文机检：改写过的「引文」必须被丢掉，且不是交给模型去判。"""
    event_id = uuid4()
    stored = [
        deepener._Doc(
            index=1,
            doc=SearchDoc(
                title="某银行 AI 审批误判",
                snippet="该行 2025 年因 AI 审批误判形成不良贷款 12 亿元，监管已介入。",
            ),
            event_id=event_id,
        )
    ]
    digest = schemas.DeepenResult(
        thesis="责任无法界定时，机构会选择不做",
        differentiator="把责任链当作落地瓶颈",
        outline=["现象", "成因", "代价"],
        counterpoints=["有机构靠流程改造解决了"],
        evidence=[
            schemas.EvidenceItem(
                doc_index=1,
                role="PRIMARY",
                quote="因 AI 审批误判形成不良贷款 12 亿元",
                claim="误判带来了真实损失",
            ),
            schemas.EvidenceItem(
                doc_index=1,
                role="SUPPORTING",
                quote="坏账高达十二个亿",  # 改写：意思对，但原文里没有这句
                claim="同一件事的另一种说法",
            ),
        ],
    )

    accepted, rejected = deepener._validate(digest, stored)

    assert rejected == 1
    assert len(accepted) == 1
    assert accepted[0].event_id == event_id
    assert accepted[0].role == "PRIMARY"
    assert accepted[0].weight == deepener.ROLE_WEIGHT["PRIMARY"]


def test_all_quotes_rejected_falls_back_to_material_text():
    """全部引文不合法 → 用材料原文兜底为 BACKGROUND，而不是假装成功。"""
    stored = [
        deepener._Doc(
            index=1,
            doc=SearchDoc(title="材料一", snippet="原文第一段", url="https://example.com/a"),
            event_id=uuid4(),
        )
    ]
    evidence = deepener._fallback(stored)
    assert len(evidence) == 1
    assert evidence[0].role == "BACKGROUND"
    assert evidence[0].quote == "原文第一段"


async def test_missing_topic_id_is_terminal(workspace):
    ws, _ = workspace
    task_id = await _add_task(ws, "deepen_topic", {})
    await _drain()

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "MISSING_TOPIC_ID"


async def test_topic_from_another_workspace_is_rejected(workspace):
    """worker 是 BYPASSRLS，跨租户引用只能靠这条显式校验拦住。"""
    ws, _ = workspace
    other_topic = uuid4()
    task_id = await _add_task(ws, "deepen_topic", {"topic_id": str(other_topic)})
    await _drain()

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "TOPIC_NOT_FOUND"


async def test_deepen_requires_a_topic_that_exists(workspace):
    ws, _ = workspace
    with pytest.raises(NonRetryableError):
        await deepener._load_topic(
            TaskContext(
                id=uuid4(),
                workspace_id=ws,
                task_type="deepen_topic",
                input={},
                retry_count=0,
                max_retries=3,
            ),
            uuid4(),
        )


async def test_select_topic_enqueues_deepen(workspace):
    """人点「选定」之后链条自己往下走：一条 dedup_key 挂在 topic 上的深入任务。"""
    ws, user_id = workspace
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)

    async with admin.begin() as conn:
        strategy_id = (
            await conn.execute(
                text(
                    "insert into strategies (workspace_id, name, status, current_version_number) "
                    "values (:ws, 'deepen-test', 'ACTIVE', 1) returning id"
                ),
                {"ws": ws},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "insert into strategy_versions "
                "(workspace_id, strategy_id, version_number, strategy_config, created_by) "
                "values (:ws, :sid, 1, '{}'::jsonb, :uid)"
            ),
            {"ws": ws, "sid": strategy_id, "uid": user_id},
        )
        topic_id = (
            await conn.execute(
                text(
                    "insert into topics (workspace_id, title, status, brief) "
                    "values (:ws, '选定即深入', 'RECOMMENDED', "
                    "cast(:brief as jsonb)) returning id"
                ),
                {
                    "ws": ws,
                    # 选定要求先过舆论检测（服务层闸门），这里直接给结论。
                    "brief": '{"opinion": {"checked": true, "verdict": "GROUNDED"}}',
                },
            )
        ).scalar_one()

    async with AsyncSession(admin, expire_on_commit=False) as session:
        topic, decision_id = await topic_service.select_topic(
            session,
            workspace_id=ws,
            topic_id=topic_id,
            user_id=user_id,
            selected_option=None,
            rationale="论点和受众都对得上",
        )
    assert topic.status == TopicStatus.SELECTED
    assert decision_id is not None

    async with WorkerSession() as session:
        tasks = list(
            (
                await session.execute(
                    select(Task).where(Task.workspace_id == ws, Task.task_type == "deepen_topic")
                )
            )
            .scalars()
            .all()
        )
    assert len(tasks) == 1
    assert tasks[0].dedup_key == f"{topic_id}:deepen"
    assert tasks[0].status == TaskStatus.READY
    assert tasks[0].input["topic_id"] == str(topic_id)

    # 再点一次会被状态机挡住，也不会长出第二条深入任务
    from xing.core.errors import ApiError

    async with AsyncSession(admin, expire_on_commit=False) as session:
        with pytest.raises(ApiError):
            await topic_service.select_topic(
                session,
                workspace_id=ws,
                topic_id=topic_id,
                user_id=user_id,
                selected_option=None,
                rationale="重复点击",
            )

    async with WorkerSession() as session:
        again = list(
            (
                await session.execute(
                    select(Task).where(Task.workspace_id == ws, Task.task_type == "deepen_topic")
                )
            )
            .scalars()
            .all()
        )
    assert len(again) == 1

    await admin.dispose()


def test_writer_puts_numbered_evidence_into_prompt():
    """写作 prompt 里必须出现可标注的证据编号，否则「[1]」无从谈起。"""
    lines = [
        writer.EvidenceLine(
            index=1,
            event_id=uuid4(),
            role="PRIMARY",
            title="AI 审批误判案",
            body="该行 2025 年因误判形成不良贷款 12 亿元",
            source="https://example.com/a",
        )
    ]
    prompt = writer._build_prompt(
        title="谁签字",
        content_type="ARTICLE",
        brief="写清楚责任链",
        research=None,
        evidence=lines,
    )

    assert "[1] PRIMARY" in prompt
    assert '原文："该行 2025 年因误判形成不良贷款 12 亿元"' in prompt
    assert "https://example.com/a" in prompt


def test_evidence_deduped_by_event_keeping_strongest_role() -> None:
    """同一篇材料只能占一行：模型会把它同时写成支撑与反驳。

    回归：真实跑里一篇「认知差距」的文章被引用两次（SUPPORTING + CONTRADICTING），
    一条 INSERT … ON CONFLICT 命中同一行两次，Postgres 直接拒绝整条语句
    （cannot affect row a second time），深入白跑一次重试。
    """
    event = uuid4()
    other = uuid4()
    evidence = [
        deepener.Evidence(
            event_id=event, role="SUPPORTING", weight=Decimal("0.7"), quote="支撑段", claim="材料一"
        ),
        deepener.Evidence(
            event_id=other, role="PRIMARY", weight=Decimal("1.0"), quote="主证据", claim="材料二"
        ),
        deepener.Evidence(
            event_id=event,
            role="CONTRADICTING",
            weight=Decimal("0.9"),
            quote="反例段",
            claim="材料一",
        ),
    ]

    kept = deepener._dedupe_evidence(evidence)

    assert [item.event_id for item in kept] == [event, other], "顺序按首次出现"
    assert kept[0].role == "CONTRADICTING", "更强角色的那条胜出"
    assert kept[0].quote == "反例段"
