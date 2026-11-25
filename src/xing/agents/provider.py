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
import json
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
        shape: dict[str, Any] | None = None,
    ) -> LlmResponse: ...


def fill(shape: Any) -> Any:
    """按 ``agents.schemas`` 的形状描述合成占位值。确定性，不依赖随机源。

    只被 ``StubProvider`` 与契约测试使用：它是「没有 key 时的模型」，
    不是业务逻辑的一部分。
    """
    if isinstance(shape, str):
        if shape.startswith("enum:"):
            return shape[5:].split("|")[0]
        return {"str": "stub", "int": 1, "float": 0.5, "bool": True}[shape]
    if isinstance(shape, list):
        return [fill(shape[0])] if shape else []
    return {key: fill(value) for key, value in shape.items()}


class StubProvider:
    """确定性 provider。

    给了 ``shape`` 就返回一个能通过 Pydantic 校验的 JSON 对象 —— 这是让
    「结构化输出」这条链路在无 key 环境里可测的唯一办法：不 stub、不 mock
    模型调用，只让这个 provider 按契约合成一个合法响应。
    没给 ``shape`` 就返回按 prompt 哈希决定的伪文本（写作类 Agent 用）。
    """

    name = "stub"

    async def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
        shape: dict[str, Any] | None = None,
    ) -> LlmResponse:
        if shape is not None:
            body = json.dumps(fill(shape), ensure_ascii=False)
        else:
            digest = hashlib.sha256(f"{system}\x00{prompt}".encode()).hexdigest()
            seed = int(digest[:8], 16)
            # 输出长度在 120~520 字之间，且完全由 prompt 决定。
            body = _pseudo_text(seed, 120 + seed % 400)

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

    def __init__(
        self, *, api_key: str, base_url: str = "", enable_thinking: bool | None = None
    ) -> None:
        # 延迟导入：不选 openai 的环境里不该因为缺包而起不来。
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        # None 表示完全不发这个字段：它是 qwen 系私有参数，发给 OpenAI 会 400。
        self._extra_body = (
            {} if enable_thinking is None else {"enable_thinking": enable_thinking}
        )

    async def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
        shape: dict[str, Any] | None = None,
    ) -> LlmResponse:
        # shape 只对 stub 有意义：真实模型靠 system 里的输出指令，
        # 把结构描述塞进 prompt 只会多花 token。
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_output_tokens,
            temperature=0.7,
            extra_body=self._extra_body or None,
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
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            enable_thinking=settings.llm_enable_thinking,
        )
    return StubProvider()
