"""输出契约的静态校验。

这一组测的不是业务逻辑，是「契约的两种表达没有漂移」：

- ``schemas.XxxModel`` 是真正的校验器；
- ``schemas.XXX_SHAPE`` 是给 stub provider 合成响应用的形状镜像。

两份东西手写就会漂。漂了的后果很隐蔽：stub 合成的 JSON 过不了校验，
于是所有用 stub 的端到端测试都会走 repair 分支 —— 测试仍然是绿的，
但它们测的已经是修复路径，不是主路径。所以这里逐对验。
"""

from __future__ import annotations

import pytest

from xing.agents.provider import fill
from xing.agents.runtime import InvalidOutput, _extract_json, _try_parse
from xing.agents.schemas import CONTRACTS


@pytest.mark.parametrize(("model_cls", "shape"), CONTRACTS)
def test_stub_shape_satisfies_the_model(model_cls, shape):
    payload = fill(shape)
    parsed = model_cls.model_validate(payload)
    assert parsed is not None


def test_extract_json_strips_code_fences():
    body = '```json\n{"verdict": "PASS"}\n```'
    assert _extract_json(body) == {"verdict": "PASS"}


def test_extract_json_survives_chatter_around_the_object():
    assert _extract_json('好的，结果如下：{"score": 3} 以上。') == {"score": 3}


def test_extract_json_rejects_text_without_an_object():
    with pytest.raises(ValueError):
        _extract_json("我无法完成这个请求。")


def test_try_parse_reports_validation_error_instead_of_raising():
    from xing.agents.schemas import ReviewVerdict

    parsed, error = _try_parse(ReviewVerdict, '{"verdict": "MAYBE", "score": 200}')
    assert parsed is None
    assert "verdict" in error


def test_invalid_output_is_not_retryable():
    from xing.core.errors import NonRetryableError

    assert issubclass(InvalidOutput, NonRetryableError)
    assert InvalidOutput.code == "INVALID_OUTPUT"
