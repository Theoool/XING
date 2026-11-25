"""v0.1 业务链的端到端测试（开发态，无登录）。

覆盖的重点不是 CRUD，而是四件容易在真实使用中坏掉的事：

1. 决策绑定的是「当前那一版战略」，不是初版。
2. 内容审批只写 approvals，contents.status 由触发器推导——两个写入口必然分叉。
3. 已通过审批的内容再改，会退回 REVIEW。
4. 换一个用户访问同一 workspace：读为空、写被拒（RLS 真的生效）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.core.config import settings
from xing.main import app

API = "/api/v1"

STRATEGY_V1 = {
    "positioning": "面向独立开发者的 AI 工具观察",
    "content_directions": ["AI 工具", "独立开发者"],
    "target_audience": ["独立开发者"],
}
STRATEGY_V2 = {
    "positioning": "面向独立开发者的 AI 工具观察（更偏实操）",
    "content_directions": ["AI 工具", "独立开发者", "成本控制"],
    "target_audience": ["独立开发者", "小团队"],
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        yield instance


@pytest.fixture
async def workspace(client: AsyncClient):
    response = await client.post(f"{API}/workspaces", json={"name": f"pytest-{uuid4()}"})
    assert response.status_code == 201, response.text
    created = response.json()

    yield created

    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text("delete from workspaces where id = :id"), {"id": created["id"]})
    await admin.dispose()


async def _create_active_strategy(client: AsyncClient, workspace_id: str) -> dict:
    response = await client.post(
        f"{API}/workspaces/{workspace_id}/strategies",
        json={"name": "v0.1 战略", "strategy_config": STRATEGY_V1},
    )
    assert response.status_code == 201, response.text
    strategy = response.json()
    assert strategy["current_version_number"] == 1

    response = await client.post(
        f"{API}/workspaces/{workspace_id}/strategies/{strategy['id']}/versions",
        json={"strategy_config": STRATEGY_V2, "change_summary": "细化方向"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["current_version_number"] == 2

    response = await client.post(
        f"{API}/workspaces/{workspace_id}/strategies/{strategy['id']}/activate"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ACTIVE"

    response = await client.get(f"{API}/workspaces/{workspace_id}/strategy")
    assert response.status_code == 200, response.text
    return response.json()


async def test_business_chain(client: AsyncClient, workspace: dict) -> None:
    workspace_id = workspace["id"]
    strategy = await _create_active_strategy(client, workspace_id)
    latest_version_id = strategy["current_version"]["id"]
    assert strategy["current_version_number"] == 2

    # --- 事件采集：同一 external_id 重复投递必须是幂等的 -----------------
    event_payload = {
        "source_type": "NEWS",
        "external_id": f"evt-{uuid4()}",
        "title": "AI 工具正在改变独立开发者的工作方式",
        "summary": "一批面向独立开发者的 AI 工具在两周内集中发布",
        "url": "https://example.com/a",
    }
    first = await client.post(f"{API}/workspaces/{workspace_id}/events/ingest", json=event_payload)
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    event_id = first.json()["event"]["id"]

    duplicate = await client.post(
        f"{API}/workspaces/{workspace_id}/events/ingest", json=event_payload
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["created"] is False
    assert duplicate.json()["event"]["id"] == event_id

    # --- 选题推荐：命中战略关键词，相关性必须大于 0 ---------------------
    recommended = await client.post(
        f"{API}/workspaces/{workspace_id}/topics/recommend", params={"limit": 5}
    )
    assert recommended.status_code == 200, recommended.text
    topics = recommended.json()
    assert len(topics) == 1
    assert topics[0]["relevance_score"] > 0, "战略关键词没命中，打分逻辑没生效"
    assert 0 < topics[0]["overall_score"] <= 1

    # 幂等：再推一次不会多出选题
    again = await client.post(f"{API}/workspaces/{workspace_id}/topics/recommend")
    assert len(again.json()) == 1

    topic_id = topics[0]["id"]

    # --- 选定选题：应该自动写一条绑定「最新版本」的决策 -----------------
    selected = await client.post(
        f"{API}/workspaces/{workspace_id}/topics/{topic_id}/select",
        json={"rationale": "与当前定位一致"},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["status"] == "SELECTED"

    decisions = await client.get(f"{API}/workspaces/{workspace_id}/decisions")
    assert decisions.status_code == 200
    rows = decisions.json()
    assert len(rows) == 1
    assert rows[0]["strategy_version_id"] == latest_version_id, "决策没有绑定到当前战略版本"
    assert rows[0]["status"] == "APPROVED"
    assert len(rows[0]["traces"]) == 1, "列表接口漏掉了决策证据"

    detail = await client.get(f"{API}/workspaces/{workspace_id}/decisions/{rows[0]['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["traces"]) == 1, "决策缺少证据记录"

    # --- 内容与版本 -----------------------------------------------------
    created = await client.post(
        f"{API}/workspaces/{workspace_id}/contents",
        json={"title": "AI 工具成本账", "topic_id": topic_id, "body": "初稿正文"},
    )
    assert created.status_code == 201, created.text
    content = created.json()
    content_id = content["id"]
    assert content["status"] == "DRAFT"
    assert content["current_version"]["version_number"] == 1

    versioned = await client.post(
        f"{API}/workspaces/{workspace_id}/contents/{content_id}/versions",
        json={"body": "改过的正文", "change_summary": "开头更直接"},
    )
    assert versioned.status_code == 201, versioned.text
    assert versioned.json()["current_version"]["version_number"] == 2

    # --- 审批：status 由触发器推导，不是接口直接改的 ---------------------
    approved = await client.post(
        f"{API}/workspaces/{workspace_id}/contents/{content_id}/approve",
        json={"comment": "可以发"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    # 通过之后再改，必须退回 REVIEW —— 审批是针对某一版的
    reworked = await client.post(
        f"{API}/workspaces/{workspace_id}/contents/{content_id}/versions",
        json={"body": "发布前又改了一版"},
    )
    assert reworked.status_code == 201, reworked.text
    assert reworked.json()["status"] == "REVIEW"

    approvals = await client.get(f"{API}/workspaces/{workspace_id}/contents/{content_id}/approvals")
    assert approvals.status_code == 200
    assert [row["status"] for row in approvals.json()] == ["APPROVED"]


async def test_other_user_is_blocked_by_rls(client: AsyncClient, workspace: dict) -> None:
    workspace_id = workspace["id"]
    await client.post(
        f"{API}/workspaces/{workspace_id}/contents", json={"title": "属于 dev 用户的内容"}
    )

    other = await client.post(
        f"{API}/dev/users", json={"email": f"other-{uuid4()}@xing.test", "display_name": "Other"}
    )
    assert other.status_code == 201, other.text
    headers = {"X-User-ID": other.json()["id"]}

    # 读：RLS 过滤，返回空列表而不是 403（不泄露「是否存在」）
    listed = await client.get(f"{API}/workspaces/{workspace_id}/contents", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []

    # 写：WITH CHECK 拒绝，42501 映射为 403
    denied = await client.post(
        f"{API}/workspaces/{workspace_id}/contents",
        json={"title": "试图写入别人的工作区"},
        headers=headers,
    )
    assert denied.status_code == 403, denied.text
    assert denied.headers["content-type"].startswith("application/problem+json")


async def test_error_contract(client: AsyncClient, workspace: dict) -> None:
    missing = await client.get(f"{API}/workspaces/{uuid4()}")
    assert missing.status_code == 404
    body = missing.json()
    assert body["code"] == "WORKSPACE_NOT_FOUND"
    assert body["status"] == 404
    assert missing.headers["content-type"].startswith("application/problem+json")

    invalid = await client.post(f"{API}/workspaces", json={"name": ""})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


async def test_event_cursor_pagination(client: AsyncClient, workspace: dict) -> None:
    workspace_id = workspace["id"]
    for index in range(3):
        response = await client.post(
            f"{API}/workspaces/{workspace_id}/events/ingest",
            json={
                "source_type": "RSS",
                "external_id": f"page-{index}-{uuid4()}",
                "title": f"第 {index} 条",
            },
        )
        assert response.status_code == 201, response.text

    first_page = await client.get(f"{API}/workspaces/{workspace_id}/events", params={"limit": 2})
    assert first_page.status_code == 200
    payload = first_page.json()
    assert len(payload["items"]) == 2
    assert payload["next_cursor"]

    second_page = await client.get(
        f"{API}/workspaces/{workspace_id}/events",
        params={"limit": 2, "cursor": payload["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["next_cursor"] is None

    ids = {item["id"] for item in payload["items"]} | {
        item["id"] for item in second_page.json()["items"]
    }
    assert len(ids) == 3, "翻页出现重复或漏项"


async def test_task_cancel_persists_reason(client: AsyncClient, workspace: dict) -> None:
    """取消任务要把 reason 写进 error（jsonb）。

    回归用例：jsonb_build_object 的参数没有类型上下文时，asyncpg 会传 unknown，
    Postgres 报 42P18（could not determine data type），接口变成 500。
    SQL 里必须显式 cast(:reason as text)。
    """
    workspace_id = workspace["id"]

    created = await client.post(
        f"{API}/workspaces/{workspace_id}/tasks",
        json={"task_type": "RENDER_VIDEO", "priority": 5, "input": {"content_id": str(uuid4())}},
    )
    assert created.status_code == 201, created.text
    task = created.json()
    # 无依赖的任务创建即 READY（触发器 trg_tasks_promote_on_insert），
    # 不再是 PENDING —— 否则队列里没人会推进它。
    assert task["status"] == "READY"
    assert task["error"] is None

    cancelled = await client.post(
        f"{API}/workspaces/{workspace_id}/tasks/{task['id']}/cancel",
        params={"reason": "用户放弃"},
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "CANCELLED"
    assert body["error"] == {"reason": "用户放弃", "cancelled_by": "user"}
    assert body["completed_at"] is not None

    again = await client.post(f"{API}/workspaces/{workspace_id}/tasks/{task['id']}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "TASK_NOT_CANCELLABLE"


async def test_task_orm_keeps_rls_and_contract(client: AsyncClient, workspace: dict) -> None:
    """ORM 试点（task_service）的验收用例。

    三件事必须同时成立，否则这次试点算失败：
    1. 响应契约不变：字段、状态、JSONB 输入输出与 raw SQL 版逐字段一致。
    2. 服务端默认值拿得回来：id(uuidv7)、created_at、status 都由库生成，
       靠 INSERT ... RETURNING 回填，不能是 None。
    3. RLS 不退化：非成员读为空、写被拒、按 id 查不到、取消不了。
       这一点尤其重要 —— ORM 换了执行路径，策略仍然要生效。
    """
    workspace_id = workspace["id"]

    created = await client.post(
        f"{API}/workspaces/{workspace_id}/tasks",
        json={"task_type": "RENDER_VIDEO", "priority": 3, "input": {"resolution": "1080p"}},
    )
    assert created.status_code == 201, created.text
    task = created.json()

    assert task["status"] == "READY"  # 无依赖 → 创建即 READY
    assert task["priority"] == 3
    assert task["input"] == {"resolution": "1080p"}
    assert task["output"] is None
    assert task["error"] is None
    assert task["started_at"] is None
    # 服务端默认值必须回填，否则说明 flush 没拿到 RETURNING
    assert task["id"] and task["created_at"], task

    fetched = await client.get(f"{API}/workspaces/{workspace_id}/tasks/{task['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == task

    listed = await client.get(f"{API}/workspaces/{workspace_id}/tasks")
    assert [item["id"] for item in listed.json()] == [task["id"]]

    other = await client.post(
        f"{API}/dev/users", json={"email": f"orm-{uuid4()}@xing.test", "display_name": "ORM"}
    )
    headers = {"X-User-ID": other.json()["id"]}

    # 读：RLS 过滤成空列表
    hidden = await client.get(f"{API}/workspaces/{workspace_id}/tasks", headers=headers)
    assert hidden.status_code == 200
    assert hidden.json() == []

    # 单条：查不到
    assert (
        await client.get(f"{API}/workspaces/{workspace_id}/tasks/{task['id']}", headers=headers)
    ).status_code == 404

    # 写：WITH CHECK 拒绝
    denied = await client.post(
        f"{API}/workspaces/{workspace_id}/tasks",
        json={"task_type": "STEAL"},
        headers=headers,
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "WORKSPACE_FORBIDDEN"

    # 改：不能绕过 RLS 取消别人的任务
    hijack = await client.post(
        f"{API}/workspaces/{workspace_id}/tasks/{task['id']}/cancel", headers=headers
    )
    assert hijack.status_code == 404

    # 仍然是 READY，说明上面那次 cancel 一个字段都没写到
    still = await client.get(f"{API}/workspaces/{workspace_id}/tasks/{task['id']}")
    assert still.json()["status"] == "READY"
