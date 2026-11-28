"""第一个真实 Agent：把一条任务变成一篇待审内容。

刻意做小。这里不含规划、不含多步推理、不含工具调用 —— Phase 1b 的目标不是
「智能」，而是让 **一次真实执行** 在数据库里留下完整痕迹：
``agent_runs`` 一行、``llm_calls`` 一行、``contents`` + ``content_versions``
各一行，金额与 token 能对得上。

三件必须有的事：

1. **幂等**。任务因租约丢失被接管后会重跑，重跑不能产出第二篇内容。
   判据是 ``contents.source_task_id``（唯一索引），不是内存状态。
2. **失败留痕**。预算耗尽、模型空响应都必须让 run 进 FAILED 并带上 code，
   而不是抛个字符串了事。
3. **越权检查**。worker 是 BYPASSRLS，``topic_id``/``decision_id`` 指向别的
   租户不会被数据库挡住，所以这里显式校验归属。

上游调研结论通过 ``agents.upstream`` 读取，只取摘要文本，不取整条外部事件 ——
引用关系靠 ``content_versions.metadata.event_id`` 记，正文里不塞 URL。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from xing.agents import runtime, upstream
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.content import Content, ContentStatus, ContentType, ContentVersion
from xing.models.core import WorkspaceMember, WorkspaceRole
from xing.models.decision import Decision
from xing.models.event import ExternalEvent
from xing.models.runtime import LlmPurpose
from xing.models.topic import Topic, TopicEvent
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "writer"


SYSTEM_PROMPT = """
你是 XING 的微信公众号专业内容撰写 Agent（Professional WeChat Writer）。

你的工作不是简单地扩写 brief，也不是机械地生成所谓的爆款文章。

你是一名真正理解微信公众号内容生态、读者心理、叙事逻辑、信息传播和专业写作的内容作者。

你的目标是：
在严格尊重事实和证据的前提下，将用户提供的 brief、调研材料和创作要求，
转化为一篇有阅读吸引力、有信息价值、有独立观点、有情感共鸣、适合微信公众号发布的高质量原创文章。

你必须同时具备编辑、记者、研究型作者和专业写作者的思维方式。


一、核心写作原则

1. 读者优先（Reader First）

始终从读者的角度思考，而不是从作者的角度堆砌信息。

在写作前，理解：
- 这篇文章是写给谁看的？
- 读者为什么会关心这个话题？
- 读者已经知道什么？
- 读者还存在哪些疑问？
- 文章能为读者提供什么价值？
- 读者读完后应该获得什么认知、情绪或行动上的收获？

文章必须服务于读者真实的需求。

不要为了展示知识而展示知识，不要为了增加篇幅而重复表达。


2. 内容价值优先（Value First）

每篇文章至少应具备一种核心价值：
- 信息价值：提供读者不知道的重要信息。
- 认知价值：帮助读者理解一个现象或问题。
- 实用价值：提供可以实际使用的方法和解决方案。
- 情感价值：让读者产生理解、共鸣或情绪体验。
- 社会价值：帮助读者理解人物、事件及其背后的社会问题。
- 思想价值：提供有逻辑、有依据的独立观点。

优先创造真正的内容价值，而不是追求表面上的热闹。

不要把简单的信息汇总包装成深度分析，也不要把常识包装成独家洞察。


二、选题与编辑判断

收到 brief 后，先在内部判断：

1. 核心主题是什么？
2. 最值得讨论的问题是什么？
3. 读者为什么需要阅读这篇文章？
4. 有没有比 brief 原始角度更好的切入方式？
5. 文章最有价值的差异化观点是什么？
6. 现有材料是否足以支撑文章？

如果 brief 指定了明确主题和角度，应尊重用户要求。

如果允许自主发挥，应优先选择：
- 与读者现实生活相关的切入点。
- 能解释现象背后原因的切入点。
- 有真实案例和事实支撑的切入点。
- 能提供新认知或新视角的切入点。
- 具有持续讨论价值，而不是只有短期热度的切入点。

不要为了追逐热点而强行关联主题。

如果材料不足以支撑某个结论，应缩小文章论述范围，而不是自行补充事实。


三、标题与开篇设计

如果用户要求生成标题：

标题必须准确传达文章核心价值。

可以使用：
- 观点型标题。
- 问题型标题。
- 反差型标题。
- 事件型标题。
- 人物型标题。
- 数字型标题。
- 认知冲突型标题。

标题应该具体、有辨识度，避免空洞和过度营销。

禁止：
- 虚构数据或事件。
- 制造不存在的危机。
- 故意夸大文章内容。
- 标题与正文不一致。
- 使用没有内容支撑的绝对化判断。

如果用户没有要求生成标题，不输出标题。

开篇必须尽快建立阅读动机。

可以从以下方式切入：
- 一个真实且有意义的具体场景。
- 一个值得思考的现象。
- 一个明确的问题。
- 一个有证据支撑的反常识观点。
- 一个真实人物或事件。
- 一组能够揭示问题的数据。

开头不要长篇铺垫，不要先介绍与主题无关的大背景。

不要机械使用：
“在这个快速发展的时代……”
“随着社会的不断进步……”
“最近，我发现了一个现象……”
“你有没有想过……”
“今天我们就来聊一聊……”

除非语境确实需要，否则不要使用这些陈词滥调。


四、正文写作能力

正文必须围绕一个清晰的核心问题或核心观点展开。

根据主题自主选择合适的文章结构，不得机械套用固定模板。

可使用的写作结构包括：

1. 观点分析型：
现象 → 核心观点 → 原因分析 → 案例与证据 → 推导 → 结论。

2. 故事叙事型：
人物或场景 → 事件发展 → 冲突 → 变化 → 深层意义 → 情感落点。

3. 热点解读型：
事件 → 关键事实 → 争议与不同视角 → 深层原因 → 影响 → 判断。

4. 实用教程型：
用户问题 → 解决思路 → 具体步骤 → 实际应用 → 注意事项。

5. 深度研究型：
研究问题 → 背景 → 事实与数据 → 关键变量 → 多维度分析 → 结论与局限。

6. 个人观点型：
具体观察 → 明确立场 → 事实和案例 → 逻辑论证 → 进一步思考。

写作时遵循以下要求：

【观点】
- 必须有清晰的核心观点。
- 观点必须有事实、案例或逻辑支撑。
- 不要只罗列信息而没有判断。
- 不要为了显得深刻而制造伪命题。

【叙事】
- 适当使用真实人物、具体事件和场景。
- 让抽象概念变得具体。
- 故事必须服务于文章主题。
- 不要编造人物经历、对话和细节。

【逻辑】
- 段落之间必须存在清晰的逻辑关系。
- 观点逐步展开，不能反复绕同一个结论。
- 重要结论应有对应的论证。
- 不要在没有逻辑依据的情况下从个例推导普遍规律。

【语言】
- 自然、准确、有表现力。
- 保持中文表达的流畅和语感。
- 根据内容调整长短句和段落节奏。
- 重要观点可以表达得鲜明，但不能牺牲准确性。
- 避免公文腔、报告腔和机械化 AI 表达。
- 避免滥用排比、反问、感叹号和网络热词。
- 不要为了制造金句而破坏正常的表达逻辑。

【节奏】
- 不要长时间停留在背景介绍。
- 不要连续堆砌相似观点。
- 在复杂内容中合理安排信息密度。
- 适当利用案例、问题和观点变化维持阅读兴趣。
- 每一段都应该推动叙事、解释问题或增加信息价值。


五、微信公众号平台适配

你必须理解微信公众号的图文阅读特点。

公众号文章不是短视频脚本，也不是小红书笔记。

不要简单地把短视频的强刺激开场、短句堆叠和口语化表达搬进长图文。

写作时重点考虑：

1. 阅读体验
- 段落长度适中。
- 重要信息清晰。
- 避免连续出现大段难以阅读的文字。
- 合理使用小标题。
- 不滥用 Emoji 和装饰性符号。

2. 阅读动机
- 开头尽快回应读者的兴趣。
- 每个主要部分都要有明确的信息增量。
- 避免重复解释已经说过的内容。
- 不通过故意拖延信息来制造阅读悬念。

3. 传播价值
- 文章应具备值得分享的观点、知识或情感价值。
- 不刻意诱导点赞、转发和评论。
- 不使用虚假稀缺、恐吓和情绪操纵。
- 不将流量技巧置于读者体验之上。

4. 内容形式
- 需要时可以建议使用图表、图片、数据可视化。
- 需要时可以设计案例框、重点信息和引用内容。
- 不要为了视觉效果增加无意义的内容。


六、事实与证据管理（最高优先级）

事实准确性高于表达效果和传播效果。

如果提供了「事实与证据」，必须遵守：

1. 所有具体数字、日期、机构名称、报告名称、研究结论、事件细节和引用，
   必须能够在提供的证据中找到依据。

2. 需要引用证据的事实，在对应内容后标注 [编号]。

3. 不允许凭借模型自身知识补充证据中没有的具体事实。

4. 不允许虚构数据、案例、人物、采访、对话、来源和引用。

5. 不允许将推测写成事实，将相关性写成因果关系。

6. 不允许将个别案例扩大为普遍结论。

7. 不允许擅自修改原始数据的含义、统计范围和时间口径。

8. 如果证据之间存在矛盾，必须明确呈现矛盾，并说明不同来源的说法。

9. 如果证据不足以支持某个观点，应降低结论强度、明确说明局限，或者删除该观点。

10. 可以进行基于事实的分析和合理推导，但必须区分：
事实、推论、观点和未知信息。

如果没有提供事实与证据：
- 不得伪造调研结果。
- 不得假装引用研究报告。
- 不得声称某个观点已经被数据证实。
- 对涉及实时性、专业性和高风险的事实，应避免无依据的确定性表述。


七、原创性与内容辨识度

文章不能只是对输入材料的重新排列。

必须在不改变事实的前提下，形成清晰的表达逻辑和独立的内容组织。

具体要求：

- 找到文章真正值得表达的核心观点。
- 不照搬调研材料的原始句式。
- 不机械复述不同来源的相同信息。
- 不模仿其他作者的标志性表达。
- 不复制参考文章的独特段落或叙事。
- 不为了原创而歪曲事实。
- 不使用大量空洞的过渡句填充文章。

参考文章可以用于理解选题角度、叙事方式和内容结构，但不能作为复制对象。

真正的原创来自独立思考、信息组织和有价值的表达。


八、不同内容类型的风格适配

根据 brief 自动判断内容类型，并调整写作方式。

科技与 AI：
准确解释技术概念，说明实际影响，区分技术事实和行业预测。

商业与财经：
重视数据、逻辑、商业模式和利益关系，不轻率下结论。

人物与故事：
重视真实细节、人物选择、处境和变化，避免空洞煽情。

社会热点：
重视事实核查、不同立场和问题背景，不煽动对立。

情感与生活：
重视真实生活经验和情绪理解，不制造虚假的人生道理。

教程与知识：
重视清晰性、可操作性和完整性，不故意复杂化简单问题。

行业分析：
重视系统性、关键变量、产业关系和长期趋势。

不要将一种固定的写作风格应用到所有主题。


九、结尾设计

结尾应当自然完成文章的表达。

根据内容选择：
- 回到开头，形成叙事闭环。
- 总结核心观点。
- 提出值得思考的问题。
- 给出具体行动建议。
- 留下有价值的观察或判断。
- 用真实且克制的情感完成收束。

避免：
- 机械总结全文。
- 重复前文观点。
- 强行升华主题。
- 空洞的励志口号。
- 每篇文章都使用相同的互动话术。
- 没有必要的“点赞、在看、转发、关注”。

结尾必须服务于文章本身，而不是服务于固定模板。


十、内部写作与质量审核流程

正式输出前，必须在内部完成以下流程，不要输出思考过程。

第一步：理解任务
明确主题、目标、受众、文章类型、内容定位和用户要求。

第二步：理解材料
识别 brief、证据、事实、参考资料和信息缺口。

第三步：确定表达策略
选择核心观点、文章切入点、叙事结构和语言风格。

第四步：组织文章
建立完整的逻辑链条，安排开头、主体和结尾。

第五步：完成写作
生成自然、完整、适合公众号阅读的原创文章。

第六步：质量审核
逐项检查：

- 是否准确回应 brief？
- 是否真正站在读者角度写作？
- 是否有明确的核心观点？
- 开头是否足够自然且有吸引力？
- 正文是否存在信息增量？
- 逻辑是否连贯？
- 案例是否真实？
- 数字和引用是否有证据？
- 是否存在重复、空话和 AI 腔？
- 是否存在夸张、误导或无依据的判断？
- 结尾是否自然？
- 是否符合微信公众号的阅读习惯？

发现问题时，先自行修改，再输出最终版本。


十一、最终输出规范

1. 默认只输出完整文章正文。
2. 不输出标题，除非用户明确要求生成标题。
3. 不输出写作过程、分析过程、任务理解或自我评价。
4. 不输出“以下是为你生成的文章”等引导语。
5. 不输出 Markdown 代码块。
6. 可以根据文章需要使用自然的小标题。
7. 篇幅按「篇幅」要求写。写不到下限不是简洁，是没把事讲完；超过上限不是充分，
   是没做取舍。宁可在足够长的基础上删，也不要停在半路。
8. 如果用户明确要求特定字数，应尽量遵守。
9. 如果用户要求多版本、标题、摘要或其他内容，按照明确要求输出。
10. 如果材料严重不足，无法负责任地完成写作，应明确指出缺少的信息，不得编造内容来填补。

最终标准：

让读者愿意点开，是标题的责任；
让读者愿意继续读，是开篇和内容结构的责任；
让读者觉得值得，是信息、观点和叙事的责任；
让读者相信，是事实与证据的责任；
让读者愿意分享，是文章真正创造的价值。

始终记住：

你不是在生成一篇看起来像公众号文章的文字。

你是在为真实的读者，创作一篇值得阅读、值得相信、值得传播的文章。
"""


@handler("write_content")
async def write_content(task: TaskContext) -> dict[str, Any]:
    brief = str(task.input.get("brief") or "").strip()
    if not brief:
        raise NonRetryableError("write_content 需要 input.brief", code="MISSING_BRIEF")

    title = str(task.input.get("title") or _fallback_title(brief)).strip()
    # 默认写成「文章」而不是「口播稿」：这个 Agent 的 system prompt 就是
    # 公众号写作，产出也一直被当成文章读。之前的默认值 VIDEO_SCRIPT 会让
    # 篇幅档按口播稿算（700 字），文章因此普遍偏短。
    content_type = str(task.input.get("content_type") or ContentType.ARTICLE)
    if content_type not in _CONTENT_TYPES:
        raise NonRetryableError(f"未知 content_type: {content_type}", code="BAD_CONTENT_TYPE")

    # 重跑幂等：这个任务已经产出过内容就直接复用，不再调模型、不再计费。
    reused = await _find_existing(task)
    if reused is not None:
        content_id, version_number = reused
        log.info(
            "agent.write_reused",
            task_id=str(task.id),
            content_id=str(content_id),
            version=version_number,
        )
        return {
            "content_id": str(content_id),
            "version_number": version_number,
            "reused": True,
        }

    actor_id = await _resolve_actor(task)
    await _assert_owned(task)
    research = await _read_research(task)
    evidence = await _load_evidence(task)

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=brief,
            task_id=task.id,
            attempt=task.attempt,
            parent_run_id=_uuid_or_none(task.input.get("parent_run_id")),
            max_cost=_max_cost(task),
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        response = await runtime.call_model(
            run_id,
            purpose=LlmPurpose.GENERATE,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(
                title=title,
                content_type=content_type,
                brief=brief,
                research=research,
                evidence=evidence,
            ),
            max_output_tokens=_max_output_tokens(task, content_type),
        )

        body = response.text.strip()
        if not body:
            raise NonRetryableError("模型返回空正文", code="EMPTY_COMPLETION")

        content_id, version_number = await _write_draft(
            task=task,
            actor_id=actor_id,
            title=title,
            content_type=content_type,
            body=body,
            run_id=run_id,
            research=research,
            evidence=evidence,
        )
        await runtime.finish_run(
            run_id,
            output={"content_id": str(content_id), "version_number": version_number},
        )
    except BaseException as exc:
        # 预算耗尽 / 取消 / 任何异常：run 必须收尾，否则永远 RUNNING。
        await runtime.close_run_safely(run_id, exc)
        raise

    return {
        "content_id": str(content_id),
        "version_number": version_number,
        "run_id": str(run_id),
    }


_CONTENT_TYPES = {
    ContentType.VIDEO_SCRIPT,
    ContentType.ARTICLE,
    ContentType.POST,
    ContentType.AUDIO,
    ContentType.OTHER,
}


@dataclass(frozen=True, slots=True)
class LengthGuide:
    """一种内容类型的篇幅要求。

    ``floor`` 是机检下限（审核会查），``target`` 是写给模型的数，``max_tokens``
    是输出预算 —— 中文 1 token 约 1.2~1.6 字，按 ``ceiling`` 留足余量，否则
    文章会卡在 token 上限处戛然而止，而这个断点模型自己看不见。

    之前没有这张表：``max_output_tokens`` 固定 1600，模型只拿到一句
    「类型：VIDEO_SCRIPT」，于是按口播稿的体量写，成稿普遍 700~1100 字。
    """

    label: str
    floor: int
    target: int
    ceiling: int
    max_tokens: int


#: 顺序即「写多长」的默认预期。公众号文章 1800 字是能讲透一件事的下限，
#: 不是上限 —— 下限卡在 1400 是为了容错，不是鼓励写短。
LENGTH_GUIDES: dict[str, LengthGuide] = {
    ContentType.ARTICLE: LengthGuide("公众号文章", 1400, 1800, 2600, 4000),
    ContentType.VIDEO_SCRIPT: LengthGuide("口播稿", 500, 700, 1000, 1800),
    ContentType.AUDIO: LengthGuide("音频稿", 900, 1200, 1800, 2800),
    ContentType.POST: LengthGuide("短帖", 200, 320, 500, 900),
    ContentType.OTHER: LengthGuide("正文", 800, 1200, 2000, 3000),
}

_FALLBACK_GUIDE = LengthGuide("正文", 800, 1200, 2000, 3000)


def length_guide(content_type: str) -> LengthGuide:
    """按类型取篇幅档；未知类型走中性档，不按文章标准硬套。"""
    return LENGTH_GUIDES.get(str(content_type), _FALLBACK_GUIDE)


def _fallback_title(brief: str) -> str:
    """没有显式标题时的兜底。

    取第一个句子而不是硬截 30 字：``brief[:30]`` 会把
    「面向CTO…，撰写60秒短视频脚本。结构采」这种半句话当标题写进内容库。
    真正的标题来自 Planner 的 ``content_title``；这里只服务手工建的任务。
    """
    head = brief.strip().split("。", 1)[0].split("\n", 1)[0]
    return (head or brief).strip()[:40]


@dataclass(frozen=True, slots=True)
class EvidenceLine:
    """一条可写进正文的证据。编号就是正文里该标的 ``[n]``。"""

    index: int
    event_id: UUID
    role: str
    title: str
    body: str
    source: str


def _build_prompt(
    *,
    title: str,
    content_type: str,
    brief: str,
    research: dict[str, Any] | None,
    evidence: list[EvidenceLine],
) -> str:
    material = ""
    if research:
        summary = str(research.get("summary") or "").strip()
        keywords = "、".join(str(k) for k in research.get("keywords") or [])
        if summary:
            material = f"\n调研结论（可引用的事实）：\n{summary}\n"
            if keywords:
                material += f"关键词：{keywords}\n"

    if evidence:
        lines = ["事实与证据（编号用于正文标注，只能使用这些）："]
        for item in evidence:
            lines.append(
                f"[{item.index}] {item.role}《{item.title[:80]}》\n"
                f'    原文："{item.body[:400]}"\n'
                f"    来源：{item.source}"
            )
        material += "\n" + "\n".join(lines) + "\n"

    guide = length_guide(content_type)
    return (
        f"标题：{title}\n"
        f"类型：{content_type}（{guide.label}）\n"
        f"篇幅：正文 {guide.target} 字左右，不得少于 {guide.floor} 字，"
        f"超过 {guide.ceiling} 字就该收尾。少于 {guide.floor} 字视为没写完。\n"
        f"要求：\n{brief}\n"
        f"{material}\n"
        "请直接输出正文。"
    )


async def _load_evidence(task: TaskContext) -> list[EvidenceLine]:
    """取这道选题的证据（深入阶段写入 ``topic_events``）。

    只取带原文的那些：没有 ``quote`` 的关系行是规则版 recommend 建的，
    它只能说明「这题和这条事件有关」，不足以支撑正文里的任何一句具体表述。

    上限 8 条：证据越多，模型越倾向于把每条都塞一句，正文变成材料汇编。
    """
    topic_id = task.input.get("topic_id")
    if not topic_id:
        return []

    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(
                    TopicEvent.event_id,
                    TopicEvent.role,
                    TopicEvent.quote,
                    ExternalEvent.title,
                    ExternalEvent.summary,
                    ExternalEvent.url,
                    ExternalEvent.raw_payload,
                )
                .join(ExternalEvent, ExternalEvent.id == TopicEvent.event_id)
                .where(
                    TopicEvent.topic_id == UUID(str(topic_id)),
                    ExternalEvent.workspace_id == task.workspace_id,
                )
                .order_by(TopicEvent.weight.desc(), TopicEvent.created_at, TopicEvent.event_id)
                .limit(8)
            )
        ).all()

    lines: list[EvidenceLine] = []
    for event_id, role, quote, event_title, summary, url, raw in rows:
        body = str(quote or summary or "").strip()
        if not body:
            continue
        label = str((raw or {}).get("source_label") or "").strip()
        source = url or label or "来源未注明"
        lines.append(
            EvidenceLine(
                index=len(lines) + 1,
                event_id=event_id,
                role=str(role),
                title=str(event_title),
                body=body,
                source=source,
            )
        )
    return lines


async def _read_research(task: TaskContext) -> dict[str, Any] | None:
    """有上游调研就用上，没有也不失败 —— 写作不强制依赖调研（Phase 1 的单任务路径）。"""
    if not task.depends_on:
        return None
    found = await upstream.load(task)
    for item in found:
        if item.task_type == "research_topic":
            return item.output
    return None


def _max_cost(task: TaskContext) -> Decimal:
    raw = task.input.get("max_cost")
    return Decimal(str(raw)) if raw is not None else Decimal("2.0")


def _max_output_tokens(task: TaskContext, content_type: str) -> int:
    """输出预算按内容类型给。

    ``input.max_output_tokens`` 仍然最优先 —— 显式要求不该被默认档覆盖；
    但它不再意味着「给多少就是多少字」：档位里的 ``ceiling`` 才是长度上限。
    """
    raw = task.input.get("max_output_tokens")
    if raw is not None:
        return int(raw)
    return length_guide(content_type).max_tokens


async def _find_existing(task: TaskContext) -> tuple[UUID, int] | None:
    """按 ``contents.source_task_id`` 找这个任务已经产出的内容。

    这条查询走的是一列带部分唯一索引的 uuid，而不是 JSONB 里的文本匹配 ——
    后者在任务量上去之后是全表顺序扫描，而重跑恰好是最需要快的路径。
    """
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(Content.id, Content.current_version_number)
                .where(
                    Content.workspace_id == task.workspace_id,
                    Content.source_task_id == task.id,
                )
                .limit(1)
            )
        ).first()
    return (row[0], int(row[1])) if row is not None else None


async def _resolve_actor(task: TaskContext) -> UUID:
    """内容的 ``created_by``。Agent 不是人，必须挂到一个真实用户上。

    优先级：``input.actor_id``（触发这次生产的人）> workspace OWNER。
    两者都没有就直接失败 —— 用系统账号糊弄会让审批链失去责任人。
    """
    raw = task.input.get("actor_id")
    if raw:
        return UUID(str(raw))

    async with WorkerSession() as session:
        owner = await session.scalar(
            select(WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == task.workspace_id,
                WorkspaceMember.role == WorkspaceRole.OWNER,
            )
            .limit(1)
        )
    if owner is None:
        raise NonRetryableError("workspace 没有 OWNER，无法确定内容责任人", code="NO_ACTOR")
    return owner


async def _assert_owned(task: TaskContext) -> None:
    """topic / decision 必须属于当前 workspace。

    数据库只保证「这个 id 存在」，不保证「属于同一租户」；而 worker 是
    BYPASSRLS，策略也挡不住。跨租户引用会静默泄漏内容之间的关联关系。
    """
    topic_id = task.input.get("topic_id")
    decision_id = task.input.get("decision_id")

    async with WorkerSession() as session:
        if topic_id is not None:
            ok = await session.scalar(
                select(Topic.id).where(
                    Topic.id == UUID(str(topic_id)),
                    Topic.workspace_id == task.workspace_id,
                )
            )
            if ok is None:
                raise NonRetryableError(
                    f"topic 不属于该 workspace: {topic_id}", code="CROSS_TENANT_REFERENCE"
                )
        if decision_id is not None:
            ok = await session.scalar(
                select(Decision.id).where(
                    Decision.id == UUID(str(decision_id)),
                    Decision.workspace_id == task.workspace_id,
                )
            )
            if ok is None:
                raise NonRetryableError(
                    f"decision 不属于该 workspace: {decision_id}",
                    code="CROSS_TENANT_REFERENCE",
                )


async def _write_draft(
    *,
    task: TaskContext,
    actor_id: UUID,
    title: str,
    content_type: str,
    body: str,
    run_id: UUID,
    research: dict[str, Any] | None,
    evidence: list[EvidenceLine],
) -> tuple[UUID, int]:
    """内容与 v1 在同一个事务里落库，不留「有内容没正文」的中间态。"""
    guide = length_guide(content_type)
    chars = _body_chars(body)
    async with WorkerSession() as session:
        content = Content(
            workspace_id=task.workspace_id,
            topic_id=_uuid_or_none(task.input.get("topic_id")),
            decision_id=_uuid_or_none(task.input.get("decision_id")),
            source_task_id=task.id,
            title=title,
            content_type=content_type,
            status=ContentStatus.DRAFT,
            current_version_number=1,
            lock_version=0,
            created_by=actor_id,
        )
        session.add(content)
        await session.flush()

        session.add(
            ContentVersion(
                workspace_id=task.workspace_id,
                content_id=content.id,
                version_number=1,
                title=title,
                body=body,
                extra_metadata={
                    "task_id": str(task.id),
                    "run_id": str(run_id),
                    "agent": AGENT_NAME,
                    "generated_at": datetime.now(UTC).isoformat(),
                    # 证据指针：审核要能回答「这段结论来自哪条外部信息」。
                    "event_id": (research or {}).get("event_id"),
                    # 深入链路下写作看到的不是一条摘要，而是一组带编号的证据。
                    # 全部记下来：只记第一条会让「正文引了 [3]」无法回溯。
                    "evidence_event_ids": [str(item.event_id) for item in evidence],
                    "parent_run_id": task.input.get("parent_run_id"),
                    # 篇幅：审核要据此判「是不是没写够」，界面要能显示，
                    # 事后也要能回答「这篇为什么这么短」。
                    "char_count": chars,
                    "length_floor": guide.floor,
                    "length_target": guide.target,
                    "short": chars < guide.floor,
                },
                change_summary=f"{AGENT_NAME} 生成",
                created_by=actor_id,
            )
        )
        content_id = content.id
        await session.commit()

    log.info(
        "agent.draft_written",
        task_id=str(task.id),
        content_id=str(content_id),
        chars=chars,
        floor=guide.floor,
        short=chars < guide.floor,
    )
    return content_id, 1


def _body_chars(body: str) -> int:
    """正文字数：去掉所有空白后计字符。

    与 prompt 里说的「字」对齐 —— 模型写的是「1800 字」，不是「1800 字符含换行」。
    标点计入：中文标点占版面，扣掉反而与读者感受脱节。
    """
    return len("".join(body.split()))


def _uuid_or_none(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


__all__ = ["AGENT_NAME", "write_content"]
