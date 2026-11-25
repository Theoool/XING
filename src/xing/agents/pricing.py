"""模型价格表与「预留上界」计算。

预算的正确性依赖一件事：**预留额必须是真实花费的上界**。
只要这条成立，结算时 ``total_cost + reserved_cost <= max_cost`` 必然仍成立，
``CHECK`` 永远不会在结算路径上触发 —— 也就不会出现「越界把明细一起回滚，
供应商收了钱而库里查无此账」的账实脱节。

所以这里刻意不用「平均 token 估算」。两个上界都是可证明的：

- 输入：BPE / SentencePiece 的一个 token 至少覆盖 1 个字符，
  所以 ``token 数 ≤ 字符数``。用字符数当输入上界，对中英文都成立。
- 输出：调用时把 ``max_output_tokens`` 传给模型，供应商不会超过它。

价格用 Decimal 而不是 float：单次调用可能低到 1e-6，
累加几百次之后 float 的误差会变成「预算明明没超却被判超」。
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

#: numeric(18,8) 的精度。预留额向上取整，避免因为截断而少留。
QUANT = Decimal("0.00000001")

#: 未列入的模型直接报错，不给「默认价」。价格猜错等于预算失效，
#: 而预算失效是静默的 —— 宁可启动失败。
DEFAULT_MAX_OUTPUT = 4096


@dataclass(frozen=True, slots=True)
class ModelPrice:
    #: 美元 / 1K tokens
    input_per_1k: Decimal
    output_per_1k: Decimal
    #: 该模型单次调用的输出上限
    max_output_tokens: int = DEFAULT_MAX_OUTPUT


class UnknownModel(RuntimeError):
    """模型不在价格表里。不允许猜测价格。"""


#: 价格会变。这里是唯一真相，改价只改这一处。
PRICES: dict[str, ModelPrice] = {
    "gpt-4o-mini": ModelPrice(Decimal("0.00015"), Decimal("0.00060"), 16384),
    "gpt-4o": ModelPrice(Decimal("0.00250"), Decimal("0.01000"), 16384),
    # 本地确定性 provider，价格纯属占位：它不产生真实费用，
    # 但预算链路必须有非零价格才能被测试到。
    "stub-1": ModelPrice(Decimal("0.00010"), Decimal("0.00040"), 2048),
    # 阿里云 MaaS（compatible-mode）上的 flash 档。单价是**预算估算用的近似值**，
    # 不是账单口径 —— 真实计费以供应商账单为准。留在这里是为了让
    # 「预留 → 结算」有确定的数字可算，而不是靠猜。
    "qwen3.7-flash": ModelPrice(Decimal("0.00005"), Decimal("0.00040"), 8192),
}


def price_of(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModel(f"模型不在价格表: {model!r}（在 agents/pricing.py 补价格）") from None


def cost_of(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """真实花费。四舍五入到 numeric(18,8)。"""
    p = price_of(model)
    return (
        (Decimal(input_tokens) * p.input_per_1k + Decimal(output_tokens) * p.output_per_1k)
        / Decimal(1000)
    ).quantize(QUANT)


def reserve_upper_bound(model: str, prompt_chars: int, max_output_tokens: int | None) -> Decimal:
    """调用前要预留的额度。

    向上取整：宁可多留一点点，也不能少留 —— 少留就会在结算时撞 CHECK。
    """
    p = price_of(model)
    out = max_output_tokens or p.max_output_tokens
    raw = (
        Decimal(prompt_chars) * p.input_per_1k + Decimal(out) * p.output_per_1k
    ) / Decimal(1000)
    return raw.quantize(QUANT, rounding=ROUND_CEILING)
