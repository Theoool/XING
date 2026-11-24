"""SQL 脚本切分。

asyncpg 走 prepared statement 协议，一次只能执行一条语句，
所以整份 DDL 脚本必须先切分。普通的 `sql.split(";")` 会在
plpgsql 函数体（$$ ... $$）内部误切，这里按 PostgreSQL 词法做状态扫描。
"""

from __future__ import annotations


def split_sql(sql: str) -> list[str]:
    """把多语句 SQL 切成单条语句，正确处理引号、注释与 dollar-quoting。"""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            end = n if end == -1 else end + 1
            buf.append(sql[i:end])
            i = end
            continue

        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue

        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i : j + 1]
                end = sql.find(tag, j + 1)
                if end == -1:
                    buf.append(sql[i:])
                    i = n
                    continue
                buf.append(sql[i : end + len(tag)])
                i = end + len(tag)
                continue

        if ch == ";":
            _flush(buf, statements)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    _flush(buf, statements)
    return statements


def _flush(buf: list[str], out: list[str]) -> None:
    stmt = "".join(buf).strip()
    if not stmt:
        return
    # 跳过纯注释片段
    meaningful = [
        line for line in stmt.splitlines() if line.strip() and not line.strip().startswith("--")
    ]
    if not meaningful:
        return
    out.append(stmt)
