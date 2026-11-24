"""RLS 上下文注入的回归测试。

针对的失败模式：
    依赖里执行一次 set_config(..., true) -> service 里 commit -> 下一条查询落到
    新事务里，GUC 为空，RLS 过滤掉全部行。
外面看起来是「创建成功，但列表查不到」，而且只在 commit 之后复现，
用单个事务包住整个用例的测试根本发现不了。

这些用例必须用受限角色（DATABASE_URL）跑；用超级用户跑会全部误判为通过。
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.core.config import settings
from xing.db.session import SessionLocal, bind_rls, db_identity


@pytest.fixture
async def dataset():
    """用迁移账号播种两个租户，测试结束清理。"""
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    ids: dict[str, UUID] = {}

    async with admin.begin() as conn:

        async def make_user(name: str) -> UUID:
            return (
                await conn.execute(
                    text(
                        "insert into users (email, display_name)"
                        " values (:email, :name) returning id"
                    ),
                    {"email": f"{name}-{uuid4()}@rls.test", "name": name},
                )
            ).scalar_one()

        alice = await make_user("alice")
        bob = await make_user("bob")

        async def make_workspace(name: str, owner: UUID) -> UUID:
            ws = (
                await conn.execute(
                    text("insert into workspaces (name, created_by) values (:n, :o) returning id"),
                    {"n": name, "o": owner},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "insert into workspace_members (workspace_id, user_id, role)"
                    " values (:w, :u, 'OWNER')"
                ),
                {"w": ws, "u": owner},
            )
            return ws

        ws_a = await make_workspace("RLS A", alice)
        ws_b = await make_workspace("RLS B", bob)

        async def make_content(ws: UUID, owner: UUID, title: str) -> UUID:
            return (
                await conn.execute(
                    text(
                        "insert into contents (workspace_id, title, status, created_by)"
                        " values (:w, :t, 'DRAFT', :o) returning id"
                    ),
                    {"w": ws, "t": title, "o": owner},
                )
            ).scalar_one()

        ids["alice"] = alice
        ids["bob"] = bob
        ids["ws_a"] = ws_a
        ids["ws_b"] = ws_b
        ids["content_a"] = await make_content(ws_a, alice, "content A")
        ids["content_b"] = await make_content(ws_b, bob, "content B")

    try:
        yield ids
    finally:
        async with admin.begin() as conn:
            await conn.execute(
                text("delete from workspaces where id in (:a, :b)"),
                {"a": ids["ws_a"], "b": ids["ws_b"]},
            )
            await conn.execute(
                text("delete from users where id in (:a, :b)"),
                {"a": ids["alice"], "b": ids["bob"]},
            )
        await admin.dispose()


async def test_app_connection_must_not_bypass_rls() -> None:
    """连接身份不对，后面所有断言都没有意义。"""
    ident = await db_identity()
    assert ident["role"] == "xing_app", f"应用角色不对：{ident}"
    assert ident["is_superuser"] is False
    assert ident["bypass_rls"] is False


async def test_context_survives_commit(dataset: dict[str, UUID]) -> None:
    """commit 之后新事务仍带着上下文——这正是依赖里手写 SET LOCAL 会漏掉的点。"""
    async with SessionLocal() as session:
        await bind_rls(session, user_id=dataset["alice"], workspace_id=dataset["ws_a"])

        first = (await session.execute(text("select id from contents"))).scalars().all()
        assert first == [dataset["content_a"]]

        await session.commit()

        guc = (
            await session.execute(text("select current_setting('app.workspace_id', true)"))
        ).scalar_one()
        assert guc == str(dataset["ws_a"]), "commit 之后 RLS 上下文丢失"

        second = (await session.execute(text("select id from contents"))).scalars().all()
        assert second == [dataset["content_a"]], "commit 之后的查询被 RLS 全部过滤"


async def test_cross_tenant_is_invisible(dataset: dict[str, UUID]) -> None:
    """拿着 A 的工作区上下文，看不到 B 的内容。"""
    async with SessionLocal() as session:
        await bind_rls(session, user_id=dataset["alice"], workspace_id=dataset["ws_a"])
        visible = (await session.execute(text("select id from contents"))).scalars().all()
        assert dataset["content_b"] not in visible

        # 冒充 bob 的 workspace，但用户仍是 alice：策略里要求成员关系，应为空
        await bind_rls(session, user_id=dataset["alice"], workspace_id=dataset["ws_b"])
        await session.commit()
        spoofed = (await session.execute(text("select id from contents"))).scalars().all()
        assert spoofed == [], "非成员拿到了别人的工作区数据"


async def test_no_context_reads_nothing(dataset: dict[str, UUID]) -> None:
    """没绑定上下文时默认拒绝，而不是全放行。"""
    async with SessionLocal() as session:
        rows = (await session.execute(text("select id from contents"))).scalars().all()
        assert rows == []


async def test_write_outside_workspace_is_rejected(dataset: dict[str, UUID]) -> None:
    """WITH CHECK 生效：不能往别人的工作区里写。"""
    async with SessionLocal() as session:
        await bind_rls(session, user_id=dataset["alice"], workspace_id=dataset["ws_a"])
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "insert into contents (workspace_id, title, status, created_by)"
                    " values (:w, 'sneaky', 'DRAFT', :o)"
                ),
                {"w": dataset["ws_b"], "o": dataset["alice"]},
            )
        await session.rollback()
