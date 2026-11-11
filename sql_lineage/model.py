"""数据模型：节点、边、语句记录。

节点 id 规则：
  - 持久对象（表 / 视图）的字段： ``<object>.<column>``，如 ``raw_orders.amount``
  - 语句内部的临时层（CTE / 派生表 / 子查询）： ``s<stmt>:<layer>.<column>``，
    如 ``s5:cte.raw_orders.amount``
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 边类型
VALUE = "value"      # 源字段的值参与计算目标字段
USAGE = "usage"      # 源字段被 WHERE/GROUP BY/JOIN ON 等引用（不直接产生值，但删除会破坏语句）

# 转换类型
DIRECT = "direct"
EXPRESSION = "expression"
AGGREGATE = "aggregate"
WINDOW = "window"
SETOP = "setop"

# 语句状态
ST_OK = "ok"
ST_PARTIAL = "partial"        # 已解析但有告警（未知字段、歧义等），血缘可能不完整
ST_UNRESOLVED = "unresolved"  # 无法解析或暂不支持
ST_IGNORED = "ignored"        # 与血缘无关的语句（SET 等）

MAX_EXPR_LEN = 500


def clip(text: str, limit: int = MAX_EXPR_LEN) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Edge:
    src: str
    dst: str
    kind: str = VALUE
    transform: str = EXPRESSION
    expression: str = ""
    location: dict = field(default_factory=dict)
    ambiguous: bool = False

    def key(self):
        return (self.src, self.dst, self.kind, self.transform, self.expression, self.ambiguous)

    def to_dict(self) -> dict:
        d = {
            "src": self.src,
            "dst": self.dst,
            "kind": self.kind,
            "transform": self.transform,
            "expression": self.expression,
            "location": self.location,
        }
        if self.ambiguous:
            d["ambiguous"] = True
        return d


@dataclass
class Node:
    id: str
    kind: str               # column | ephemeral
    object: str = ""        # 所属对象名（持久列）或层前缀（临时节点）
    column: str = ""
    statement: str = ""     # 临时节点所属的语句 id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "object": self.object,
            "column": self.column,
            "statement": self.statement,
        }


@dataclass
class StatementRecord:
    id: str
    file: str
    line: int
    kind: str = ""            # create_table | create_view | insert | ctas | drop | other
    status: str = ST_OK
    target: str = ""
    summary: str = ""
    reasons: list = field(default_factory=list)   # 未解析原因
    warnings: list = field(default_factory=list)  # {level, msg}
    refs: list = field(default_factory=list)      # 引用到的表名（尽力而为，供影响分析提示）

    def warn(self, msg: str, level: str = "warn"):
        self.warnings.append({"level": level, "msg": msg})

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "status": self.status,
            "target": self.target,
            "summary": self.summary,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "refs": self.refs,
        }


@dataclass
class ObjectDef:
    """一张表或视图的定义。"""

    name: str
    kind: str = "table"       # table | view
    columns: list = field(default_factory=list)  # [{name, type}]
    source: str = "sql"       # manifest | sql
    defined_by: dict = field(default_factory=dict)

    def column_names(self):
        return [c["name"] for c in self.columns]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "columns": self.columns,
            "source": self.source,
            "defined_by": self.defined_by,
        }
