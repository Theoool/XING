"""内容服务（ORM 版）。

审批的唯一写入口是 approvals 表：
    approve / reject 只插入审批记录，``contents.status`` 由数据库触发器推导。
    这条规则由 0003 迁移里的触发器 + 守卫函数强制，服务层不重复实现，
    所以这里看不到「顺便把 status 改一下」的代码 —— 那是双写的开始。

因此 approve 之后必须 ``populate_existing=True`` 重查一次才能拿到新状态：
会话是 ``expire_on_commit=False``，commit 不会让对象失效，
不强制刷新的话你读到的还是审批前的 status，接口会返回过期的值。

``add_version`` 是唯一例外：它主动把 APPROVED 退回 REVIEW，因为审批是针对
某一版做的，换了版本原审批就失效了。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from xing.core.errors import ApiError
from xing.models.content import (
    Approval,
    ApprovalStage,
    ApprovalStatus,
    Content,
    ContentStatus,
    ContentVersion,
)
from xing.schemas.content import (
    ApprovalOut,
    ContentCreate,
    ContentOut,
    ContentVersionOut,
)


def _to_out(content: Content) -> ContentOut:
    """详情用：带当前版本（joined eager load 已就绪，不会触发懒加载）。"""
    return ContentOut.model_validate(content)


def _to_summary(content: Content) -> ContentOut:
    """列表用：不带正文。

    ``current_version`` 显式给 None，而不是靠 loader option 把关系属性变成 None。
    后者在 SQLAlchemy 2.1 已被标记为「会返回错误结果」，且会持续告警；
    而「列表不返回正文」本来就是接口契约（见 docs/API.md），显式表达更准确。
    """
    return ContentOut(
        id=content.id,
        topic_id=content.topic_id,
        decision_id=content.decision_id,
        title=content.title,
        content_type=content.content_type,
        status=content.status,
        current_version_number=content.current_version_number,
        lock_version=content.lock_version,
        created_at=content.created_at,
        updated_at=content.updated_at,
        current_version=None,
    )


async def create_content(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    payload: ContentCreate,
) -> ContentOut:
    content = Content(
        workspace_id=workspace_id,
        topic_id=payload.topic_id,
        decision_id=payload.decision_id,
        title=payload.title,
        content_type=payload.content_type,
        status=ContentStatus.DRAFT,
        current_version_number=1,
        lock_version=0,
        created_by=user_id,
    )
    session.add(content)
    await session.flush()

    # 有正文就同时落 v1，避免出现「有内容没版本」的中间态
    if payload.body:
        session.add(
            ContentVersion(
                workspace_id=workspace_id,
                content_id=content.id,
                version_number=1,
                title=payload.title,
                body=payload.body,
                change_summary="initial",
                created_by=user_id,
            )
        )

    await session.commit()
    return await get_content(session, workspace_id=workspace_id, content_id=content.id)


async def get_content(session: AsyncSession, *, workspace_id: UUID, content_id: UUID) -> ContentOut:
    content = (
        await session.execute(
            select(Content)
            .where(Content.id == content_id, Content.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if content is None:
        raise ApiError(404, "CONTENT_NOT_FOUND", "内容不存在")
    return _to_out(content)


async def list_contents(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    status: str | None,
    topic_id: UUID | None,
    limit: int,
) -> list[ContentOut]:
    stmt = (
        select(Content)
        # 列表刻意不带当前版本：正文可能有几十 KB，列表页不需要。
        # lazyload 把默认的 joined eager load 降级，避免把 content_versions 拖进来；
        # 下面用 _to_summary 显式构造，不碰关系属性，所以不会触发懒加载。
        .options(lazyload(Content.current_version))
        .where(Content.workspace_id == workspace_id)
        .order_by(Content.updated_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Content.status == status)
    if topic_id:
        stmt = stmt.where(Content.topic_id == topic_id)

    contents = (await session.execute(stmt)).scalars().all()
    return [_to_summary(content) for content in contents]


async def add_version(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    content_id: UUID,
    user_id: UUID,
    body: str,
    title: str | None,
    change_summary: str | None,
) -> ContentOut:
    content = (
        await session.execute(
            select(Content)
            # 同 strategy：FOR UPDATE 不能作用在 LEFT JOIN 的可空侧
            .options(lazyload(Content.current_version))
            .where(Content.id == content_id, Content.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if content is None:
        raise ApiError(404, "CONTENT_NOT_FOUND", "内容不存在")
    if content.status == ContentStatus.ARCHIVED:
        raise ApiError(409, "CONTENT_ARCHIVED", "已归档的内容不能新增版本")

    next_version = content.current_version_number + 1
    next_title = title or content.title

    session.add(
        ContentVersion(
            workspace_id=workspace_id,
            content_id=content_id,
            version_number=next_version,
            title=next_title,
            body=body,
            change_summary=change_summary,
            created_by=user_id,
        )
    )

    # 已通过审批的内容被改动后必须回到 REVIEW：审批是针对某一版做的，
    # 换了版本，原来的通过就失效了。
    if content.status == ContentStatus.APPROVED:
        content.status = ContentStatus.REVIEW

    content.current_version_number = next_version
    content.title = next_title
    content.lock_version = Content.lock_version + 1

    await session.commit()
    return await get_content(session, workspace_id=workspace_id, content_id=content_id)


async def _lock_content(session: AsyncSession, workspace_id: UUID, content_id: UUID) -> Content:
    content = (
        await session.execute(
            select(Content)
            .options(lazyload(Content.current_version))
            .where(Content.id == content_id, Content.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if content is None:
        raise ApiError(404, "CONTENT_NOT_FOUND", "内容不存在")
    if content.status == ContentStatus.ARCHIVED:
        raise ApiError(409, "CONTENT_ARCHIVED", "已归档的内容不能审核")
    return content


async def _record_approval(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    content_id: UUID,
    user_id: UUID,
    approve: bool,
    comment: str | None,
) -> None:
    # 先作废还在等的那张单，避免 PENDING 与新结论并存
    # （approvals_one_pending_per_stage 是部分唯一索引，两条 PENDING 会直接撞）
    await session.execute(
        update(Approval)
        .where(
            Approval.content_id == content_id,
            Approval.stage == ApprovalStage.CONTENT,
            Approval.status == ApprovalStatus.PENDING,
        )
        .values(status=ApprovalStatus.CANCELLED, decided_at=func.now())
        .execution_options(synchronize_session=False)
    )

    session.add(
        Approval(
            workspace_id=workspace_id,
            content_id=content_id,
            stage=ApprovalStage.CONTENT,
            status=ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED,
            reviewer_id=user_id,
            comment=comment,
            decided_at=func.now(),
        )
    )


async def approve_content(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    content_id: UUID,
    user_id: UUID,
    comment: str | None,
) -> ContentOut:
    content = await _lock_content(session, workspace_id, content_id)
    if content.status == ContentStatus.APPROVED:
        raise ApiError(409, "CONTENT_ALREADY_APPROVED", "内容已通过审核")

    await _record_approval(
        session,
        workspace_id=workspace_id,
        content_id=content_id,
        user_id=user_id,
        approve=True,
        comment=comment,
    )
    await session.commit()
    # 提交后 contents.status 已由触发器改成 APPROVED，必须强制重查才能读到
    return await get_content(session, workspace_id=workspace_id, content_id=content_id)


async def reject_content(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    content_id: UUID,
    user_id: UUID,
    comment: str | None,
) -> ContentOut:
    await _lock_content(session, workspace_id, content_id)

    await _record_approval(
        session,
        workspace_id=workspace_id,
        content_id=content_id,
        user_id=user_id,
        approve=False,
        comment=comment,
    )
    await session.commit()
    return await get_content(session, workspace_id=workspace_id, content_id=content_id)


async def list_versions(
    session: AsyncSession, *, workspace_id: UUID, content_id: UUID
) -> list[ContentVersionOut]:
    versions = (
        (
            await session.execute(
                select(ContentVersion)
                .where(
                    ContentVersion.content_id == content_id,
                    ContentVersion.workspace_id == workspace_id,
                )
                .order_by(ContentVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ContentVersionOut.model_validate(version) for version in versions]


async def list_approvals(
    session: AsyncSession, *, workspace_id: UUID, content_id: UUID
) -> list[ApprovalOut]:
    approvals = (
        (
            await session.execute(
                select(Approval)
                .where(
                    Approval.content_id == content_id,
                    Approval.workspace_id == workspace_id,
                )
                .order_by(Approval.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ApprovalOut.model_validate(approval) for approval in approvals]
