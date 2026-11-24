"""开发辅助接口。

只在 APP_ENV=development 下可用。存在的理由很具体：
开发阶段不做登录，但验证「租户隔离」需要第二个真实用户，
而 users.id 是外键，随便编一个 UUID 会直接违反约束。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from xing.core.config import settings
from xing.core.errors import ApiError
from xing.db.session import SessionLocal
from xing.models.core import User

router = APIRouter(prefix="/dev", tags=["dev"])


class DevUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=100)


class DevUserOut(BaseModel):
    id: str
    email: str
    display_name: str


@router.post("/users", response_model=DevUserOut, status_code=201, summary="创建测试用户（仅开发）")
async def create_dev_user(payload: DevUserCreate) -> DevUserOut:
    if not settings.is_dev:
        raise ApiError(404, "NOT_FOUND", "接口不存在")

    email = payload.email.strip().lower()
    if "@" not in email:
        raise ApiError(422, "INVALID_EMAIL", "邮箱格式不正确")

    async with SessionLocal() as session:
        # 唯一索引建在 lower(email) 上，查询也用 lower(email) 才能命中索引，
        # 否则会退化成全表扫。
        user = (
            await session.execute(select(User).where(func.lower(User.email) == email))
        ).scalar_one_or_none()

        if user is not None:
            user.display_name = payload.display_name
        else:
            user = User(email=email, display_name=payload.display_name)
            session.add(user)

        await session.commit()
        # flush 发生在 commit 内，uuidv7() 生成的主键此时已回填到对象上
        return DevUserOut(id=str(user.id), email=email, display_name=payload.display_name)
