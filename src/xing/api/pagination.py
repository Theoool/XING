"""游标分页。

为什么不用 offset：外部事件、性能快照这类表会到几十万行，
OFFSET 越翻越慢，而且 COUNT(*) 每次都是全表扫。
用 (created_at, id) 的键集分页，翻到第几页都是同一个代价。

游标对客户端是不透明字符串（base64），避免前端依赖内部字段顺序。
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from uuid import UUID

from xing.core.errors import ApiError


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ts_text, id_text = raw.split("|", 1)
        return datetime.fromisoformat(ts_text), UUID(id_text)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiError(422, "INVALID_CURSOR", "分页游标无效") from exc
