"""LLM provider 抽象。

只有一个方法 ``complete``，因为 Runtime 需要的就这一件事：给上下文、拿文本
和 token 数。工具调用、结构化输出都放在上层用 prompt + Pydantic 校验解决，
直到真的需要函数调用再扩 —— 提前抽象会先锁死一条还不存在的路径。

两个实现：

- :class:`StubProvider` 确定性、不联网。测试与本地开发默认用它。
  它对同一个输入永远给同一个输出，所以「录制回放」能被真实测试到。
- :class:`OpenAIProvider` 走官方 SDK。只在显式配置且有 key 时启用。

``request`` / ``response`` 是**脱敏后的完整往返**，直接落 ``llm_calls``：
它们既是回放的燃料，也是事后唯一能解释「模型当时看到了什么」的证据。
"""

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

from xing.core.config import settings


@dataclass(frozen=True, slots=True)
class LlmResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    #: 落库用。不含思维链，也不含任何凭据。
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)


class LlmProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
    ) -> LlmResponse: ...


class StubProvider:
    """确定性 provider。同一个 prompt 永远得到同一段文本。

    token 数按字符数估（上界），这样预算测试不会因为估值偏小而在结算时撞 CHECK。
    """

    name = "stub"

    async def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
    ) -> LlmResponse:
        digest = hashlib.sha256(f"{system}\x00{prompt}".encode()).hexdigest()
        seed = int(digest[:8], 16)

        # 输出长度在 120~520 字之间，且完全由 prompt 决定。
        length = 120 + seed % 400
        body = _pseudo_text(seed, length)

        return LlmResponse(
            text=body,
            input_tokens=len(system) + len(prompt),
            output_tokens=len(body),
            model=model,
            request={"system": system, "prompt": prompt, "max_output_tokens": max_output_tokens},
            response={"text": body, "finish_reason": "stop", "stub": True},
        )


def _pseudo_text(seed: int, length: int) -> str:
    """由 seed 决定的伪文本。不追求可读，只追求确定且长度可控。"""
    pool = (
        "取舍的关键在于把不确定的东西变成可观测的指标而不是更长的计划"
        "内容的价值来自判断力重复劳动应该交给系统决定权留给人"
    )
    start = seed % len(pool)
    rotated = pool[start:] + pool[:start]
    return (rotated * (length // len(rotated) + 1))[:length]


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, api_key: str, base_url: str = "") -> None:
        # 延迟导入：不选 openai 的环境里不该因为缺包而起不来。
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)

    async def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
    ) -> LlmResponse:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_output_tokens,
            temperature=0.7,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = resp.usage
        if usage is None:
            raise RuntimeError("provider 未返回 usage，无法记账 —— 拒绝在无成本信息的调用上继续")

        return LlmResponse(
            text=text,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            model=model,
            request={"system": system, "prompt": prompt, "max_output_tokens": max_output_tokens},
            response={"text": text, "finish_reason": choice.finish_reason},
        )


@lru_cache(maxsize=1)
def get_provider() -> LlmProvider:
    """按配置选 provider。默认 stub —— 没有 key 也能把整条链路跑通。"""
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("llm_provider=openai 但没有 openai_api_key")
        return OpenAIProvider(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    return StubProvider()
