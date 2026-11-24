"""不依赖外部服务的冒烟测试。需要真实 PG/Redis 的用例请标注 @pytest.mark.integration。"""

import pytest
from httpx import ASGITransport, AsyncClient

from xing.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    # ASGITransport 不触发 lifespan，因此不会在单测里连数据库
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_v1_ping(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/ping")
    assert resp.status_code == 200
    assert resp.json()["message"] == "pong"
