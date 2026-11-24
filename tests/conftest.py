"""pytest 全局夹具。"""

import pytest

from xing.db.session import dispose_engine


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    """每个用例结束后释放连接池。

    pytest-asyncio 默认给每个用例新建事件循环，而连接池是模块级全局的：
    上一个用例留下的连接绑定在已经关闭的循环上，下个用例复用时就会在
    close/abort 阶段抛 "Event loop is closed"。清空池子比换 loop 作用域更省事，
    也顺带保证用例之间没有连接状态残留（例如残留的 RLS 上下文）。
    """
    yield
    await dispose_engine()
