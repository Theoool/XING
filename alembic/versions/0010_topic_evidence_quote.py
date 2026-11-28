"""证据层：给 ``topic_events`` 加 ``quote``。

深入这条环的输出不是「一段更好的摘要」，而是三样能被机器检查的东西：

1. 证据原文（``topic_events.quote``）—— 引文必须是检索材料的子串；
2. 证据角色（``role``：PRIMARY / SUPPORTING / CONTRADICTING / BACKGROUND）；
3. 证据强度（``weight``）。

``role`` 与 ``weight`` 0002 就建好了，``quote`` 一直缺。缺它的后果很具体：
审核 Agent 只能拿「一条摘要」当事实基线，无法判断正文里那句话是引来的
还是编出来的 —— 上一轮真实模型编出「某银行千万级坏账」，就是在这个缺口上
发生的。

只有一列，没有新表。理由：证据是「选题 ↔ 外部事件」这条关系的属性，
关系表已经在了，再加一张 ``topic_evidence`` 会让「这个选题有哪些证据」
有两个答案。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_topic_evidence_quote"
down_revision: str | None = "0009_topic_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE topic_events ADD COLUMN quote text")
    op.execute(
        "COMMENT ON COLUMN topic_events.quote IS "
        "'该证据的原文片段；落库前已校验它是 external_events.summary 的子串'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE topic_events DROP COLUMN quote")
