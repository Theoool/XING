"""Agent Runtime：step 循环、预算预留、调用录制。

这一层只做四件事，每件都对应一个「不做就会失真」的理由：

1. **step 是决策循环，不是调用次数。** :func:`begin_step` 用一条条件 UPDATE
   原子地递增；0 行影响就是超预算。没有「先读后写」的窗口，也不需要应用层 if。
2. **钱要先占后花。** :func:`call_model` 在调用前预留额度（条件 UPDATE），
   调用后释放预留并写明细。预留额是真实花费的上界（见 ``pricing``），
   所以结算永远不会撞 ``CHECK`` —— 也就不会出现「明细被回滚、账实脱节」。
3. **LLM 调用在事务之外。** 三个事务（预留 / 结算 / 失败收尾）各自独立，
   调用期间不持有连接。否则一次 30 秒的模型调用会同时：阻塞租约回收、
   占满连接池、废掉 ``SKIP LOCKED`` 的并发优势。
4. **每次调用都落 ``llm_calls``。** 包括失败与回放命中。成本明细是唯一
   能回答「钱花在哪」的数据；只记成功的调用，等于给账本挑好看的记。

失败与预算耗尽抛出的是 :class:`xing.core.errors.NonRetryableError` 子类
（预算）或原样透传（provider 抖动）—— worker 据此决定重试还是终态。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from xing.agents.pricing import cost_of, price_of, reserve_upper_bound
from xing.agents.provider import LlmProvider, LlmResponse, get_provider
from xing.core.config import settings
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.runtime import (
    AgentRun,
    AgentRunStatus,
    AutonomyMode,
    LlmCall,
    LlmCallStatus,
    LlmPurpose,
)

log = structlog.get_logger(__name__)

#: 预留额里额外算给 system prompt 与模板包装的字符数
PROMPT_OVERHEAD_CHARS = 32


class RunError(NonRetryableError):
    code = "RUN_ERROR"


class RunNotRunning(RunError):
    """run 已终结或不存在。继续调用没有意义。"""

    code = "RUN_NOT_RUNNING"


class BudgetExceeded(RunError):
    """金额预算不足。重试不会变出钱来，直接终态。"""

    code = "BUDGET_EXCEEDED"


class StepBudgetExceeded(BudgetExceeded):
    code = "STEP_BUDGET_EXCEEDED"


class InvalidOutput(RunError):
    """结构化输出两次都不合法。重试没有意义：同样的 prompt 会给同样的垃圾。"""

    code = "INVALID_OUTPUT"


@dataclass(frozen=True, slots=True)
class RunSpec:
    workspace_id: UUID
    agent_name: str
    goal: str
    task_id: UUID | None = None
    parent_run_id: UUID | None = None
    attempt: int = 1
    autonomy_mode: str = AutonomyMode.SEMI_AUTO
    max_steps: int = 12
    max_tokens: int = 200_000
    max_cost: Decimal = Decimal("2.0")


async def start_run(spec: RunSpec) -> UUID:
    """开一次执行。这一步之后 ``agent_runs`` 里就有了一行 —— 审计的起点。"""
    async with WorkerSession() as session:
        run = AgentRun(
            workspace_id=spec.workspace_id,
            task_id=spec.task_id,
            parent_run_id=spec.parent_run_id,
            agent_name=spec.agent_name,
            attempt=spec.attempt,
            goal=spec.goal[:2000],
            autonomy_mode=spec.autonomy_mode,
            max_steps=spec.max_steps,
            max_tokens=spec.max_tokens,
            max_cost=spec.max_cost,
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    log.info(
        "agent.run_started",
        run_id=str(run_id),
        agent=spec.agent_name,
        attempt=spec.attempt,
        max_cost=str(spec.max_cost),
    )
    return run_id


async def begin_step(run_id: UUID) -> int:
    """开始一个决策循环，返回本次的 step 序号（从 1 起）。

    「能不能再走一步」由 ``WHERE step_count < max_steps`` 判定。条件不满足时
    0 行影响 —— 没有读写窗口，两个并发的 step 也不会各自看到同一个余额。
    """
    async with WorkerSession() as session:
        value = await session.scalar(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING,
                AgentRun.step_count < AgentRun.max_steps,
            )
            .values(step_count=AgentRun.step_count + 1)
            .returning(AgentRun.step_count)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    if value is None:
        await _explain_step_rejection(run_id)
    return int(value)  # type: ignore[arg-type]


async def _explain_step_rejection(run_id: UUID) -> None:
    """把「0 行影响」翻译成准确原因。诊断信息不准会让人查错方向。"""
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(AgentRun.status, AgentRun.step_count, AgentRun.max_steps).where(
                    AgentRun.id == run_id
                )
            )
        ).first()

    if row is None:
        raise RunNotRunning(f"run 不存在: {run_id}")
    status, step_count, max_steps = row
    if status != AgentRunStatus.RUNNING:
        raise RunNotRunning(f"run 已终结（status={status}）")
    raise StepBudgetExceeded(f"步数预算耗尽（{step_count}/{max_steps}）")


def make_replay_key(
    provider: str,
    model: str,
    system: str,
    prompt: str,
    *,
    web_search: bool = False,
    json_object: bool = False,
) -> str:
    """录制回放的键。租户维度由唯一索引承担，不放进键里。

    同一个 workspace 内，同 provider / 同模型 / 同输入 才算「同一个请求」。
    ``web_search`` 必须进键：同一段 prompt 带不带联网是两次不同的事实来源，
    回放命中错的那个会把「没联网的回答」当成「检索结果」写进证据层。

    版本号从 ``v2`` 升到 ``v3``，因为键的构成变了（多了 JSON 模式这一维）。
    顺带的好处：此前录下的那批坏输出不会再被命中，不必手工清缓存。
    """
    digest = hashlib.sha256(
        "\x00".join(
            [
                "v3",
                provider,
                model,
                "web" if web_search else "text",
                "json" if json_object else "raw",
                system,
                prompt,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return digest


async def call_model(
    run_id: UUID,
    *,
    purpose: str,
    step_no: int,
    prompt: str,
    system: str = "",
    provider: LlmProvider | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    allow_replay: bool = True,
    shape: dict[str, Any] | None = None,
    web_search: bool = False,
    prompt_overhead: int | None = None,
    temperature: float | None = None,
    json_object: bool = False,
) -> LlmResponse:
    """预留 → 调用（事务外）→ 结算。三步各自独立事务。

    ``shape`` 只被 stub provider 使用（合成一个合法响应）；真实模型靠
    system prompt 里的输出指令。它不进 ``replay_key``：形状由 Agent 代码决定，
    不是请求内容，进键只会让「同一请求」被拆成两个键。

    ``web_search`` 是模型端联网检索。它有两个额外后果：回放键要区分带不带联网；
    预留额度必须加上供应商注入的检索上下文（``prompt_overhead``），
    否则「预留是花费上界」不成立 —— 实测同一段 prompt 的 prompt_tokens
    会因为检索正文从几百涨到七千。

    ``json_object`` 与 ``temperature`` 同样进键：开了 JSON 模式的那次和没开的，
    是两次不同的请求，输出形状也不同。少了这一维，回放可能把「随手写的一段话」
    交给一个指望 JSON 的调用方。
    """
    prov = provider or get_provider()
    mdl = model or settings.llm_model
    max_out = max_output_tokens or price_of(mdl).max_output_tokens
    replay_key = make_replay_key(
        prov.name, mdl, system, prompt, web_search=web_search, json_object=json_object
    )
    overhead = PROMPT_OVERHEAD_CHARS if prompt_overhead is None else prompt_overhead
    amount = reserve_upper_bound(mdl, len(system) + len(prompt) + overhead, max_out)

    # ---- 1. 预留（事务内）---------------------------------------------
    async with WorkerSession() as session:
        workspace_id = await session.scalar(
            select(AgentRun.workspace_id).where(AgentRun.id == run_id)
        )
        if workspace_id is None:
            raise RunNotRunning(f"run 不存在: {run_id}")

        if allow_replay:
            replayed = await _replay(
                session, run_id, workspace_id, replay_key, purpose, step_no, prov.name, mdl
            )
            if replayed is not None:
                await session.commit()
                log.info(
                    "agent.call_replayed",
                    run_id=str(run_id),
                    step=step_no,
                    purpose=purpose,
                    key=replay_key[:12],
                )
                return replayed

        reserved = await session.scalar(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == AgentRunStatus.RUNNING,
                AgentRun.total_cost + AgentRun.reserved_cost + amount <= AgentRun.max_cost,
            )
            .values(reserved_cost=AgentRun.reserved_cost + amount)
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        if reserved is None:
            status = await session.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
            if status != AgentRunStatus.RUNNING:
                raise RunNotRunning(f"run 已终结（status={status}）")
            budget = (
                await session.execute(
                    select(
                        AgentRun.total_cost,
                        AgentRun.reserved_cost,
                        AgentRun.max_cost,
                    ).where(AgentRun.id == run_id)
                )
            ).one()
            raise BudgetExceeded(
                "金额预算不足："
                f"已花 {budget[0]} + 已预留 {budget[1]} + 需要 {amount} > 上限 {budget[2]}"
            )

        await session.commit()

    # ---- 2. 调用（事务外，不持连接）-----------------------------------
    started = time.perf_counter()
    try:
        response = await prov.complete(
            model=mdl,
            system=system,
            prompt=prompt,
            max_output_tokens=max_out,
            shape=shape,
            web_search=web_search,
            temperature=temperature,
            json_object=json_object,
        )
    except BaseException as exc:
        # 取消 / 网络抖动 / 供应商 5xx 都到这里。明细仍然要落 —— 失败调用
        # 可能已经计费，账本不能只记成功的那部分。
        await _settle_failure(
            run_id=run_id,
            workspace_id=workspace_id,
            amount=amount,
            purpose=purpose,
            step_no=step_no,
            provider=prov.name,
            model=mdl,
            replay_key=replay_key,
            exc=exc,
        )
        raise
    latency_ms = int((time.perf_counter() - started) * 1000)
    # 记住这次响应挂在哪把回放键上：上层解析不过时要靠它把这条输出从缓存里摘掉。
    # 少了这两个字段，一份坏输出会被重试原样复读 —— 错误位置都一样，看起来像
    # 模型每次都错在同一处，其实是我们在复读。
    response = replace(response, replay_key=replay_key, workspace_id=workspace_id)

    # ---- 3. 结算（事务内）---------------------------------------------
    actual = cost_of(mdl, response.input_tokens, response.output_tokens)
    try:
        await _settle_success(
            run_id=run_id,
            workspace_id=workspace_id,
            amount=amount,
            purpose=purpose,
            step_no=step_no,
            provider=prov.name,
            model=mdl,
            replay_key=replay_key,
            response=response,
            actual=actual,
            latency_ms=latency_ms,
        )
    except DBAPIError:
        # 预留是上界，理论上到不了这里。真到了说明价格表或供应商 usage 有问题，
        # 此时最重要的是把预留释放掉，否则这个 run 永远无法结束。
        log.error("agent.settle_failed", run_id=str(run_id), actual=str(actual), exc_info=True)
        await _release(run_id, amount)
        raise

    log.info(
        "agent.call_succeeded",
        run_id=str(run_id),
        step=step_no,
        purpose=purpose,
        tokens=response.input_tokens + response.output_tokens,
        cost=str(actual),
        latency_ms=latency_ms,
    )
    return response


async def call_structured[ModelT: BaseModel](
    run_id: UUID,
    *,
    purpose: str,
    step_no: int,
    system: str,
    prompt: str,
    schema: type[ModelT],
    shape: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> ModelT:
    """要一个**能通过校验**的结构化输出。

    失败只给一次修复机会（``purpose='repair'``），因为修复用的是同一条
    预算与同一个 step：再多的自动重试也只是把「模型看不明白这个 schema」
    这件事拖到更贵的地方。两次都不合法 → ``INVALID_OUTPUT``，run 终态。

    修复调用花的是真钱，所以它也走 :func:`call_model`，也就同样被预留/结算
    与 ``llm_calls`` 覆盖 —— 不因为它叫"修复"就绕开账本。

    **回放命中的坏输出不算一次失败。** 复读一段当时就没通过校验的文本，
    既不花模型的钱也得不到新信息；先把它从缓存里摘掉再真问一次，这是
    同一个请求的第一次真正执行，不该消耗修复机会。
    """

    async def ask(text: str, purpose_name: str, *, allow_replay: bool) -> LlmResponse:
        return await call_model(
            run_id,
            purpose=purpose_name,
            step_no=step_no,
            system=system,
            prompt=text,
            max_output_tokens=max_output_tokens,
            shape=shape,
            allow_replay=allow_replay,
            # 结构化输出走两道锁：解码层强制 JSON（``json_object``），
            # 随机性压到接近贪心。0.7 对「长嵌套 JSON 里的引号」这件事太松了 ——
            # 实测同一步连续两次都写坏，一次把数组写成了键值对，一次漏了引号。
            json_object=True,
            temperature=0.2,
        )

    response = await ask(prompt, purpose, allow_replay=True)
    parsed, error = _try_parse(schema, response.text)
    if parsed is None and response.replayed:
        await forget_replay(response)
        response = await ask(prompt, purpose, allow_replay=False)
        parsed, error = _try_parse(schema, response.text)
    if parsed is not None:
        return parsed

    # 解析不过的输出不能留在回放缓存里：否则重试拿到的是同一段坏文本，
    # 「第 20 行」这种位置每次都一样 —— 看起来像模型固执地错在同一处，
    # 实际上是我们在复读自己录下的那次失败。
    await forget_replay(response)
    log.warning(
        "agent.output_invalid",
        run_id=str(run_id),
        step=step_no,
        purpose=purpose,
        error=error.partition("\n")[0],
    )
    repaired = await ask(
        (
            f"{prompt}\n\n"
            "上一次输出无法解析为约定结构。解析器给的位置、以及出错处附近的原文：\n"
            f"{error}\n\n"
            "请据此修正：只输出一个合法 JSON 对象；所有字符串值都要用双引号包起来，"
            "不能在字符串里直接换行；不要 Markdown 代码块，不要任何解释。"
        ),
        LlmPurpose.REPAIR,
        allow_replay=True,
    )
    parsed, error = _try_parse(schema, repaired.text)
    if parsed is not None:
        return parsed

    await forget_replay(repaired)
    # 第一行是给人看的一句话（任务失败码里的 message），后面几行是给模型的修复线索。
    first_line = error.partition("\n")[0]
    raise InvalidOutput(f"结构化输出两次不合法（{schema.__name__}）：{first_line}")


def _try_parse[ModelT: BaseModel](schema: type[ModelT], text: str) -> tuple[ModelT | None, str]:
    """解析并校验。返回 (值, 错误描述)，二选一。

    错误描述的**第一行**是给人看的一句话（直接进 ``INVALID_OUTPUT`` 的 message），
    后面几行是给模型看的修复线索：出错位置、附近原文、常见原因。
    只告诉模型「第 20 行第 1 列解析失败」，它改不动 —— 它得先看到自己写坏的那两行。
    """
    try:
        return schema.model_validate(_extract_json(text)), ""
    except json.JSONDecodeError as exc:
        value, fixed = _parse_with_quote_repair(schema, text)
        if value is not None:
            return value, ""
        return None, _json_hint(exc, text, repaired=fixed)
    except (ValueError, ValidationError) as exc:
        return None, str(exc)[:1500]


def _parse_with_quote_repair[ModelT: BaseModel](
    schema: type[ModelT], text: str
) -> tuple[ModelT | None, bool]:
    """给「键后换行、值裸写」补引号，再解析一次。

    这是唯一一处容错，收得很窄：只补引号，不改结构、不搬字段、不猜缺了什么。
    补完必须**重新经过 ``json.loads`` 与 schema 校验**，过不了就当没补过 ——
    所以最坏情况是回到原来的报错，不会把一段能用的输出判死，也不会把一段坏的
    拼成看起来合法的东西。

    为什么值得补：实测 qwen 写长中文值时反复这么干 ——
    ``"claim":`` 换行之后直接跟一句中文。整段输出除了这一点完全正确，
    finish_reason 还是 ``stop``。为这个关键字让整条链失败，代价远大于补一个引号。
    补了就记日志，不静默。
    """
    fixed = _quote_bare_values(text)
    if fixed is None:
        return None, False
    try:
        value = schema.model_validate(_extract_json(fixed))
    except (ValueError, ValidationError):
        return None, False
    log.warning("agent.json_repaired", schema=schema.__name__, kind="unquoted_value")
    return value, True


def _quote_bare_values(text: str) -> str | None:
    """把 ``"key":`` 独占一行时后面那几行裸文本变成带引号的字符串。

    值里已经带引号、或者值其实是对象/数组/数字的，一律不动 —— 那些情况加引号
    会把语义改错，宁可交回给模型重答。
    """
    lines = text.splitlines()
    out: list[str] = []
    touched = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _is_key_only(line):
            out.append(line)
            index += 1
            continue

        cursor = index + 1
        chunk: list[str] = []
        while cursor < len(lines) and not _starts_structure(lines[cursor]):
            chunk.append(lines[cursor].strip())
            cursor += 1
        if not chunk or any('"' in part for part in chunk):
            out.append(line)
            index += 1
            continue

        out.append(f'{line.rstrip()} "{" ".join(chunk)}"')
        touched = True
        index = cursor

    return "\n".join(out) if touched else None


def _is_key_only(line: str) -> bool:
    stripped = line.strip()
    return stripped.endswith(":") and stripped.startswith('"') and stripped.count('"') == 2


def _starts_structure(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped[0] in "{}[]":
        return True
    return stripped.startswith('"') and ":" in stripped


def _json_hint(exc: json.JSONDecodeError, text: str, *, repaired: bool = False) -> str:
    """把「第 N 行」翻译成模型能据以修改的东西。"""
    lines = text.splitlines()
    index = min(max(exc.lineno - 1, 0), max(len(lines) - 1, 0))
    window = "\n".join(lines[max(0, index - 1) : index + 2])[:400]
    note = (
        "注意：已经尝试给裸值补过引号，仍然不合法 —— 所以问题不在引号，"
        "更可能是括号/逗号错位，或者把某个数组写成了键值对。\n"
        if repaired
        else ""
    )
    return (
        f"{exc}\n"
        f"{note}"
        "出错位置的附近原文（不含行号）：\n"
        f"{window}\n"
        "常见原因：字符串值没加双引号、在字符串里直接换行、多写或漏写逗号/方括号。"
    )


def _extract_json(text: str) -> Any:
    """从模型输出里取出那个 JSON 对象。

    只做「剥掉代码块围栏 + 取首尾大括号之间」这两件确定的事，不做容错猜测：
    真正的容错是让模型重答一次，而不是写一个能吞下任意垃圾的解析器。
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]

    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出里没有 JSON 对象")
    return json.loads(body[start : end + 1])


async def record_plan(run_id: UUID, plan: dict[str, Any]) -> None:
    """把计划写进 ``agent_runs.plan``。

    计划必须落库而不是只留在内存：审计时要能回答「这次执行当初打算做什么」，
    而事后拿着最终产物去反推计划是做不到的。
    """
    async with WorkerSession() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(plan=plan)
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def finish_run(run_id: UUID, *, output: dict[str, Any] | None = None) -> bool:
    """正常收尾。返回 False 表示 run 已经不在 RUNNING（被取消或已被收尾）。"""
    async with WorkerSession() as session:
        ok = await session.scalar(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == AgentRunStatus.RUNNING)
            .values(
                status=AgentRunStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                output=output,
                reserved_cost=Decimal(0),
            )
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    if ok is not None:
        log.info("agent.run_succeeded", run_id=str(run_id))
    return ok is not None


async def fail_run(run_id: UUID, *, code: str, message: str) -> bool:
    """异常收尾。预留额度一并释放，避免失败之后余额被永久占住。"""
    async with WorkerSession() as session:
        ok = await session.scalar(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == AgentRunStatus.RUNNING)
            .values(
                status=AgentRunStatus.FAILED,
                completed_at=datetime.now(UTC),
                error={"code": code, "message": message[:2000]},
                reserved_cost=Decimal(0),
            )
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    if ok is not None:
        log.warning("agent.run_failed", run_id=str(run_id), code=code)
    return ok is not None


async def close_run_safely(run_id: UUID, exc: BaseException) -> None:
    """handler 中途异常时收尾 run。取消场景下尽力而为，绝不二次抛异常。

    被取消（租约丢失）时，``await`` 在本任务里会立刻抛 ``CancelledError``，
    但 ``shield`` 里的协程仍会在事件循环上跑完 —— 这是「尽力而为」的准确含义：
    可能延迟一点，但比留下一个永远 RUNNING 的僵尸 run 好。
    """
    code = getattr(exc, "code", None) or type(exc).__name__
    with contextlib.suppress(BaseException):
        await asyncio.shield(fail_run(run_id, code=str(code), message=str(exc)))


# ---------------------------------------------------------------- internals


async def _replay(
    session: Any,
    run_id: UUID,
    workspace_id: UUID,
    replay_key: str,
    purpose: str,
    step_no: int,
    provider: str,
    model: str,
) -> LlmResponse | None:
    """查同租户内已录制的响应。命中就写一条 REPLAYED 明细（cost=0）并返回。"""
    hit = (
        await session.execute(
            select(LlmCall.request, LlmCall.response, LlmCall.model_name)
            .where(
                LlmCall.workspace_id == workspace_id,
                LlmCall.replay_key == replay_key,
                LlmCall.run_id != run_id,
                LlmCall.response.is_not(None),
                LlmCall.status.in_((LlmCallStatus.SUCCEEDED, LlmCallStatus.REPLAYED)),
            )
            .order_by(LlmCall.started_at.desc())
            .limit(1)
        )
    ).first()
    if hit is None:
        return None

    request, response, model_name = hit
    seq = await _next_call_seq(session, run_id)
    session.add(
        LlmCall(
            workspace_id=workspace_id,
            run_id=run_id,
            purpose=purpose,
            attempt=await _attempt_of(session, run_id),
            step_no=step_no,
            call_seq=seq,
            model_provider=provider,
            model_name=model_name,
            input_tokens=0,
            output_tokens=0,
            cost=Decimal(0),
            latency_ms=0,
            status=LlmCallStatus.REPLAYED,
            completed_at=datetime.now(UTC),
            request=request,
            response=response,
            replay_key=replay_key,
        )
    )
    await session.flush()

    text = (response or {}).get("text", "")
    return LlmResponse(
        text=text,
        input_tokens=0,
        output_tokens=0,
        model=model_name,
        request=request or {},
        response=response or {},
        replay_key=replay_key,
        workspace_id=workspace_id,
        replayed=True,
    )


async def forget_replay(response: LlmResponse) -> None:
    """把一份解析不过的输出从回放缓存里摘掉。

    回放的本意是「同一个请求不重复付钱」，不是「同一个错误不重复犯」。
    命中缓存时我们只确认过「那次 HTTP 成功了」，从没确认过「那段文本能用」；
    于是一份坏输出被永久复读 —— 重试连报错的字符位置都一样，看着像模型固执，
    其实是我们在放录音。摘掉键之后，下一次同样的请求会真的去问模型，
    新响应再接管这把键（唯一索引只约束非空键，所以不会冲突）。

    清缓存失败不该把一次还能重试的运行变成终态，所以异常只记日志。
    """
    key = response.replay_key
    if not key or response.workspace_id is None:
        return
    try:
        async with WorkerSession() as session:
            await session.execute(
                update(LlmCall)
                .where(LlmCall.workspace_id == response.workspace_id, LlmCall.replay_key == key)
                .values(replay_key=None)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception:
        log.warning("agent.replay_forget_failed", key=key[:12], exc_info=True)


async def _next_call_seq(session: Any, run_id: UUID) -> int:
    value = await session.scalar(
        select(func.coalesce(func.max(LlmCall.call_seq), 0) + 1).where(LlmCall.run_id == run_id)
    )
    return int(value or 1)


async def _attempt_of(session: Any, run_id: UUID) -> int:
    value = await session.scalar(select(AgentRun.attempt).where(AgentRun.id == run_id))
    return int(value or 1)


async def _settle_success(
    *,
    run_id: UUID,
    workspace_id: UUID,
    amount: Decimal,
    purpose: str,
    step_no: int,
    provider: str,
    model: str,
    replay_key: str,
    response: LlmResponse,
    actual: Decimal,
    latency_ms: int,
) -> None:
    """先释放预留、再插明细。顺序不能反。

    触发器在 INSERT 时把 ``actual`` 累加进 ``total_cost``；如果先插明细，
    那一瞬间 ``total_cost + reserved_cost`` 会把同一笔钱算两遍。
    """
    async with WorkerSession() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(reserved_cost=func.greatest(AgentRun.reserved_cost - amount, 0))
            .execution_options(synchronize_session=False)
        )
        seq = await _next_call_seq(session, run_id)
        session.add(
            LlmCall(
                workspace_id=workspace_id,
                run_id=run_id,
                purpose=purpose,
                attempt=await _attempt_of(session, run_id),
                step_no=step_no,
                call_seq=seq,
                model_provider=provider,
                model_name=model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost=actual,
                latency_ms=latency_ms,
                status=LlmCallStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                request=response.request,
                response=response.response,
                replay_key=replay_key,
            )
        )
        await session.commit()


async def _settle_failure(
    *,
    run_id: UUID,
    workspace_id: UUID,
    amount: Decimal,
    purpose: str,
    step_no: int,
    provider: str,
    model: str,
    replay_key: str,
    exc: BaseException,
) -> None:
    """失败也要留明细：供应商可能已经计费，账本不能只记成功的调用。"""

    async def _run() -> None:
        async with WorkerSession() as session:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(reserved_cost=func.greatest(AgentRun.reserved_cost - amount, 0))
                .execution_options(synchronize_session=False)
            )
            seq = await _next_call_seq(session, run_id)
            session.add(
                LlmCall(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    purpose=purpose,
                    attempt=await _attempt_of(session, run_id),
                    step_no=step_no,
                    call_seq=seq,
                    model_provider=provider,
                    model_name=model,
                    input_tokens=0,
                    output_tokens=0,
                    cost=Decimal(0),
                    status=LlmCallStatus.FAILED,
                    completed_at=datetime.now(UTC),
                    error={"code": type(exc).__name__, "message": str(exc)[:2000]},
                    replay_key=replay_key,
                )
            )
            await session.commit()

    with contextlib.suppress(BaseException):
        await asyncio.shield(_run())


async def _release(run_id: UUID, amount: Decimal) -> None:
    async with WorkerSession() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(reserved_cost=func.greatest(AgentRun.reserved_cost - amount, 0))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
