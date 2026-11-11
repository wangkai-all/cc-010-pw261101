"""把 SQL 文件切分成单条语句。

不能简单按分号 split：字符串字面量、注释、美元引号里都可能出现分号。
这里用一个小型状态机处理 DuckDB 的字符串/标识符/注释语法，
同时记录每条语句的起始行号，供血缘边标注来源位置。
"""
from __future__ import annotations


def split_statements(text: str) -> list:
    """返回 [(statement_sql, start_line)]，start_line 从 1 开始。"""
    stmts = []
    buf = []
    line = 1
    stmt_line = None  # 第一条非空白字符所在行
    has_code = False  # 是否包含注释以外的内容
    i, n = 0, len(text)
    state = None  # None | squote | dquote | backtick | line_comment | block_comment | dollar

    def flush():
        nonlocal buf, stmt_line, has_code
        sql = "".join(buf).strip()
        if sql and has_code:
            stmts.append((sql, stmt_line if stmt_line is not None else line))
        buf = []
        stmt_line = None
        has_code = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "'":
                state = "squote"
                has_code = True
                buf.append(ch)
            elif ch == '"':
                state = "dquote"
                has_code = True
                buf.append(ch)
            elif ch == "`":
                state = "backtick"
                has_code = True
                buf.append(ch)
            elif ch == "-" and nxt == "-":
                state = "line_comment"
                buf.append(ch)
                i += 1
                buf.append(nxt)
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                buf.append(ch)
                i += 1
                buf.append(nxt)
            elif ch == "$" and nxt == "$":
                state = "dollar"
                has_code = True
                buf.append("$$")
                i += 1
            elif ch == ";":
                flush()
            else:
                if not ch.isspace():
                    if stmt_line is None:
                        stmt_line = line
                    has_code = True
                buf.append(ch)
        elif state == "squote":
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # 两个连续单引号是转义
                    buf.append(nxt)
                    i += 1
                else:
                    state = None
        elif state == "dquote":
            buf.append(ch)
            if ch == '"':
                state = None
        elif state == "backtick":
            buf.append(ch)
            if ch == "`":
                state = None
        elif state == "line_comment":
            buf.append(ch)
            if ch == "\n":
                state = None
        elif state == "block_comment":
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 1
                state = None
        elif state == "dollar":
            if ch == "$" and nxt == "$":
                buf.append("$$")
                i += 1
                state = None
            else:
                buf.append(ch)
        if ch == "\n":
            line += 1
        i += 1
    flush()
    return stmts
