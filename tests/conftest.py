"""pytest 全局夹具。"""

import os

# 测试永远跑 stub，与开发者本地的 .env 无关。
#
# 之前这两件事是耦合的：把 .env 切成真实模型好让界面能用，测试就会跟着
# 变成「真的调模型、真的花钱、还不可复现」。强制在导入应用之前改写环境变量，
# 让「界面连真模型」和「测试离线可复现」同时成立。
# 必须在 import xing.* 之前执行 —— settings 是模块级单例，导入即固化。
os.environ["LLM_PROVIDER"] = "stub"
os.environ["LLM_MODEL"] = "stub-1"
os.environ["SEARCH_PROVIDER"] = "stub"

import pytest  # noqa: E402

from xing.db.session import dispose_engine  # noqa: E402


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
