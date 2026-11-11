"""分析器：驱动多趟解析、循环依赖检测，汇总成报告。

多趟解析：语句可以按任意顺序引用视图/CTAS 的结果。每一趟尝试解析所有
待处理语句，能解析的就提交（其产物可能被后续语句引用），直到一轮没有
进展。剩下的语句要么是引用了未知对象，要么构成了循环依赖。
"""
from __future__ import annotations

import datetime
import json
import re

import sqlglot
from sqlglot import exp

from .model import (
    ST_IGNORED,
    ST_OK,
    ST_PARTIAL,
    ST_UNRESOLVED,
    Node,
    StatementRecord,
)
from .resolver import (
    DIALECT,
    Txn,
    UnknownObject,
    UnsupportedConstruct,
    resolve_statement,
    table_full_name,
)
from .schema import SchemaRegistry, norm_name
from .splitter import split_statements

# 与血缘无关的语句类型：跳过但不影响完整性
_IGNORED_TYPES = (exp.Set, exp.Use, exp.Pragma, exp.Command, exp.Describe,
                  exp.TruncateTable, exp.Commit, exp.Rollback, exp.Show)


def _first_code_line(sql: str) -> str:
    """摘要取第一条非注释行（前导注释不算）。"""
    for line in sql.strip().splitlines():
        s = line.strip()
        if s and not s.startswith("--"):
            return s[:80]
    return sql.strip()[:80]


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_error(msg: str) -> str:
    """sqlglot 的错误信息带 ANSI 高亮和多行上下文，只保留单行纯文本。"""
    return _ANSI_RE.sub("", str(msg)).split("\n", 1)[0][:200]


class _Stmt:
    """待解析语句的工作单元。"""

    def __init__(self, record: StatementRecord, ast):
        self.record = record
        self.ast = ast
        self.missing = ""  # 上一次尝试时缺失的对象名


class Analyzer:
    def __init__(self, dialect: str = DIALECT):
        self.dialect = dialect
        self.schema = SchemaRegistry()
        self.nodes: dict = {}
        self.edges: list = []
        self._edge_keys: set = set()
        self.statements: list[StatementRecord] = []
        self.cycles: list[dict] = []
        self.files: list[str] = []
        self._stmt_counter = 0
        self._pending: list[_Stmt] = []

    # ------------------------------------------------------------------ 输入

    def load_manifest(self, data: dict, source: str = "manifest"):
        warnings = self.schema.load_manifest(data, {"file": source, "line": 0})
        for w in warnings:
            self._meta_warning(w)

    def load_manifest_file(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.load_manifest(json.load(f), source=path)

    def add_sql_text(self, text: str, filename: str = "<inline>"):
        if filename not in self.files:
            self.files.append(filename)
        for sql, line in split_statements(text):
            self._stmt_counter += 1
            stmt_id = f"s{self._stmt_counter}"
            record = StatementRecord(id=stmt_id, file=filename, line=line)
            self.statements.append(record)
            record.summary = _first_code_line(sql)
            try:
                ast = sqlglot.parse_one(sql, read=self.dialect)
            except Exception as e:
                record.status = ST_UNRESOLVED
                record.kind = "unknown"
                record.reasons.append(f"SQL 解析失败: {_clean_error(e)}")
                continue
            if ast is None:
                record.status = ST_IGNORED
                record.kind = "empty"
                continue
            record.refs = self._extract_refs(ast)
            if isinstance(ast, _IGNORED_TYPES):
                record.status = ST_IGNORED
                record.kind = type(ast).__name__.lower()
                continue
            self._pending.append(_Stmt(record, ast))

    def add_sql_file(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.add_sql_text(f.read(), filename=path)

    # ------------------------------------------------------------------ 运行

    def run(self) -> dict:
        pending = self._pending
        while True:
            progress = False
            still: list[_Stmt] = []
            for stmt in pending:
                rec = stmt.record
                txn = Txn(rec.id, rec.file, rec.line)
                try:
                    info = resolve_statement(stmt.ast, self.schema, txn, self.dialect)
                except UnknownObject as e:
                    stmt.missing = e.name
                    still.append(stmt)
                    continue
                except UnsupportedConstruct as e:
                    rec.status = ST_UNRESOLVED
                    rec.reasons.append(str(e))
                    progress = True  # 状态有变化，但它不会产生新对象
                    continue
                except Exception as e:  # 防御：解析器内部错误不应拖垮整个分析
                    rec.status = ST_UNRESOLVED
                    rec.reasons.append(f"内部分析错误: {type(e).__name__}: {e}")
                    continue
                self._commit(txn, rec, info)
                progress = True
            pending = still
            if not progress or not pending:
                break
        self._pending = pending
        self._classify_failures(pending)
        return self._build_report()

    def _commit(self, txn: Txn, record: StatementRecord, info: dict):
        for name in txn.unregistrations:
            self.schema.unregister(name)
        for obj in txn.registrations:
            warning = self.schema.register(obj)
            if warning:
                txn.warn(warning, level="info")
        for node_id, node in txn.nodes.items():
            self.nodes.setdefault(node_id, node)
        for edge in txn.edges:
            if edge.key() not in self._edge_keys:
                self._edge_keys.add(edge.key())
                self.edges.append(edge)
        record.kind = info.get("kind", "")
        record.target = info.get("target", "")
        record.warnings.extend(txn.warnings)
        record.status = ST_PARTIAL if any(w["level"] == "warn" for w in txn.warnings) else ST_OK

    # ------------------------------------------------------------------ 失败分类与循环检测

    def _classify_failures(self, pending: list[_Stmt]):
        if not pending:
            return
        # 每条剩余语句定义的对象名 / 引用的对象名
        defined = {}
        for stmt in pending:
            names = set()
            ast = stmt.ast
            if isinstance(ast, exp.Create):
                this = ast.this
                table = this.this if isinstance(this, exp.Schema) else this
                if isinstance(table, exp.Table):
                    names.add(norm_name(table_full_name(table)))
            elif isinstance(ast, exp.Insert):
                target = ast.this
                table = target.this if isinstance(target, exp.Schema) else target
                if isinstance(table, exp.Table):
                    names.add(norm_name(table_full_name(table)))
            defined[stmt.record.id] = names

        refs = {stmt.record.id: set(stmt.record.refs) for stmt in pending}

        # 剩余语句之间的依赖图：A 引用了 B 定义的对象 → A -> B
        graph: dict[str, list[str]] = {sid: [] for sid in defined}
        for sid, rset in refs.items():
            for other, dset in defined.items():
                if sid == other:
                    if rset & dset:
                        graph[sid].append(other)  # 自引用
                    continue
                if rset & dset or {r.rsplit(".", 1)[-1] for r in rset} & {d.rsplit(".", 1)[-1] for d in dset}:
                    graph[sid].append(other)

        in_cycle: set[str] = set()
        for cycle_path in _find_cycles(graph):
            names = []
            for sid in cycle_path:
                in_cycle.add(sid)
                names.append(sorted(defined[sid])[0] if defined[sid] else sid)
            names.append(names[0])
            self.cycles.append({
                "path": names,
                "statements": list(cycle_path),
            })

        for stmt in pending:
            rec = stmt.record
            rec.status = ST_UNRESOLVED
            if rec.id in in_cycle:
                for cyc in self.cycles:
                    if rec.id in cyc["statements"]:
                        rec.reasons.append(
                            "检测到循环依赖: " + " → ".join(cyc["path"]))
                        break
            else:
                missing = stmt.missing or "、".join(sorted(refs[rec.id])) or "未知"
                rec.reasons.append(
                    f"引用了未定义的对象 '{missing}'（schema 清单和 SQL 中均未找到）")

    # ------------------------------------------------------------------ 报告

    def _meta_warning(self, msg):
        self.statements.append(StatementRecord(
            id="meta", file="", line=0, kind="meta",
            status=ST_PARTIAL, warnings=[{"level": "warn", "msg": msg}]))

    def _extract_refs(self, ast) -> list:
        """尽力提取语句引用的表名（排除语句内定义的 CTE 与 CREATE 目标），供影响分析提示。"""
        cte_names = set()
        for cte in ast.find_all(exp.CTE):
            alias = cte.args.get("alias")
            if alias is not None and alias.this is not None:
                cte_names.add(str(alias.this.name).lower())
        excluded = set(cte_names)
        if isinstance(ast, exp.Create):  # CREATE 的目标表不是"引用"
            this = ast.this
            table = this.this if isinstance(this, exp.Schema) else this
            if isinstance(table, exp.Table):
                excluded.add(norm_name(table_full_name(table)))
        refs = []
        seen = set()
        for t in ast.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                continue
            name = norm_name(table_full_name(t))
            if name in excluded or name.rsplit(".", 1)[-1] in excluded:
                continue
            if name not in seen:
                seen.add(name)
                refs.append(name)
        return refs

    def _build_report(self) -> dict:
        # 为所有已知对象的字段补齐持久节点（清单里的表可能只出现在边的端点上）
        nodes = dict(self.nodes)
        for name, obj in self.schema.objects.items():
            for col in obj.columns:
                nid = f"{name}.{col['name']}"
                if nid not in nodes:
                    nodes[nid] = Node(id=nid, kind="column", object=name,
                                      column=col["name"])
        statements = [s.to_dict() for s in self.statements]
        unresolved = [s for s in statements if s["status"] == ST_UNRESOLVED]
        partial = [s for s in statements if s["status"] == ST_PARTIAL]
        return {
            "meta": {
                "tool": "sql_lineage",
                "dialect": self.dialect,
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "files": self.files,
            },
            "objects": {name: obj.to_dict() for name, obj in sorted(self.schema.objects.items())},
            "nodes": {nid: n.to_dict() for nid, n in sorted(nodes.items())},
            "edges": [e.to_dict() for e in self.edges],
            "statements": statements,
            "cycles": self.cycles,
            "stats": {
                "objects": len(self.schema.objects),
                "nodes": len(nodes),
                "edges": len(self.edges),
                "statements": len([s for s in statements if s["kind"] != "meta"]),
                "ok": len([s for s in statements if s["status"] == ST_OK]),
                "partial": len(partial),
                "unresolved": len(unresolved),
                "cycles": len(self.cycles),
                "complete": not unresolved and not partial and not self.cycles,
            },
        }


def _find_cycles(graph: dict) -> list[list[str]]:
    """在 {节点: [后继]} 图中找简单环，返回节点 id 列表（每个环一条路径）。"""
    cycles = []
    state = {}  # 0=未访问 1=在栈中 2=已完成

    def dfs(start):
        stack = [(start, iter(graph.get(start, [])))]
        path = [start]
        state[start] = 1
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if state.get(nxt) == 1:
                    idx = path.index(nxt)
                    cycles.append(path[idx:])
                elif state.get(nxt, 0) == 0:
                    state[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(graph.get(nxt, []))))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                path.pop()
                state[node] = 2

    for node in graph:
        if state.get(node, 0) == 0:
            dfs(node)
    # 去重（同一个环可能从不同入口被发现）
    unique = []
    seen = set()
    for cyc in cycles:
        key = frozenset(cyc)
        if key not in seen:
            seen.add(key)
            unique.append(cyc)
    return unique
