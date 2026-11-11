"""核心解析器：把单条 SQL 语句解析成字段级血缘边。

设计要点：
  - 基于 sqlglot 的 AST 做作用域解析，字符串字面量、注释天然不会被误判为表/字段；
  - 每个查询层（语句顶层、CTE、派生表、子查询）都会为输出字段建立节点，
    边只连接相邻两层，因此下游路径能还原完整的中间链路；
  - CTE 名称遮蔽基础表、内层 CTE 遮蔽外层 CTE，通过作用域链自然解决；
  - SELECT * 按该层 FROM 来源的字段顺序展开，支持 DuckDB 的 EXCLUDE/REPLACE/RENAME；
  - 解析结果先写入事务（Txn），语句整体成功后才由 Analyzer 提交，失败可安全重试。
"""
from __future__ import annotations

import re

from sqlglot import exp

from .model import (
    AGGREGATE,
    DIRECT,
    EXPRESSION,
    SETOP,
    USAGE,
    VALUE,
    WINDOW,
    Edge,
    Node,
    ObjectDef,
    clip,
)
from .schema import SchemaRegistry

DIALECT = "duckdb"


class UnknownObject(Exception):
    """引用了 schema 清单和已解析 SQL 中都不存在的表/视图。"""

    def __init__(self, name):
        self.name = name
        super().__init__(name)


class UnsupportedConstruct(Exception):
    """语句使用了暂不支持的语法。"""


def arg(node, *names):
    """兼容不同 sqlglot 版本的参数名（with_/with、from_/from）。"""
    for n in names:
        if n in node.args:
            return node.args[n]
    return None


def norm_part(value) -> str:
    """标识符规范化：未加引号的统一小写，加引号的保留大小写。"""
    if isinstance(value, exp.Identifier):
        return value.name if value.quoted else value.name.lower()
    if value is None:
        return ""
    return str(value).lower()


def ident_of(node) -> str:
    if isinstance(node, exp.Identifier):
        return norm_part(node)
    if isinstance(node, exp.Column):
        return ident_of(node.this)
    return norm_part(node)


def table_full_name(table: exp.Table) -> str:
    parts = [
        norm_part(table.args.get("catalog")),
        norm_part(table.args.get("db")),
        norm_part(table.this),
    ]
    return ".".join(p for p in parts if p)


def sql_text(node, dialect: str = DIALECT) -> str:
    try:
        return clip(node.sql(dialect=dialect))
    except Exception:
        return clip(str(node))


class Txn:
    """单条语句解析过程的暂存区；语句成功后才提交，失败整体丢弃。"""

    def __init__(self, stmt_id: str, file: str, line: int):
        self.stmt_id = stmt_id
        self.location = {"file": file, "line": line, "statement": stmt_id}
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._edge_keys: set = set()
        self.warnings: list[dict] = []
        self.registrations: list[ObjectDef] = []
        self.unregistrations: list[str] = []

    def warn(self, msg: str, level: str = "warn"):
        self.warnings.append({"level": level, "msg": msg})

    def add_node(self, node_id: str, kind: str, object_: str, column: str):
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(
                id=node_id,
                kind=kind,
                object=object_,
                column=column,
                statement=self.stmt_id if kind == "ephemeral" else "",
            )

    def add_edge(self, src: str, dst: str, kind: str, transform: str,
                 expression: str = "", ambiguous: bool = False):
        edge = Edge(
            src=src, dst=dst, kind=kind, transform=transform,
            expression=clip(expression), location=dict(self.location),
            ambiguous=ambiguous,
        )
        if edge.key() not in self._edge_keys:
            self._edge_keys.add(edge.key())
            self.edges.append(edge)


class OutCol:
    """一个查询层的输出字段。"""

    __slots__ = ("name", "node")

    def __init__(self, name, node):
        self.name = name
        self.node = node


class POut:
    """投影展开后的中间结果（尚未分配节点 id）。"""

    __slots__ = ("name", "deps", "transform", "expression")

    def __init__(self, name, deps, transform, expression):
        self.name = name
        self.deps = deps            # [(node_id, ambiguous)]
        self.transform = transform
        self.expression = expression


class Source:
    """FROM 子句里的一个数据来源。"""

    __slots__ = ("alias", "prefix", "kind", "columns")

    def __init__(self, alias, prefix, kind, columns):
        self.alias = alias
        self.prefix = prefix        # 字段节点 id 前缀（对象名或临时层 id）
        self.kind = kind
        self.columns = columns      # [(name, node_id)]，保持顺序供星号展开

    def column_map(self):
        return dict(self.columns)


class Scope:
    """一层查询的作用域；parent 指向外层（相关子查询/嵌套 CTE 用）。"""

    def __init__(self, parent=None):
        self.parent = parent
        self.sources: list[Source] = []
        self.by_alias: dict[str, Source] = {}
        self.ctes: dict[str, Source] = {}  # 本层 WITH 定义的 CTE（遮蔽外层对象）
        self.using_cols: set = set()       # JOIN USING 的合并字段（多来源同名不算歧义）

    def add_source(self, src: Source):
        self.sources.append(src)
        self.by_alias[src.alias] = src

    def chain(self):
        sc = self
        while sc is not None:
            yield sc
            sc = sc.parent


class QueryResolver:
    def __init__(self, schema: SchemaRegistry, txn: Txn, dialect: str = DIALECT):
        self.schema = schema
        self.txn = txn
        self.dialect = dialect
        self.seq = 0
        self.used_layers: set[str] = set()

    # ------------------------------------------------------------------ 工具

    def new_layer(self, hint: str) -> str:
        self.seq += 1
        safe = re.sub(r"[^0-9A-Za-z_.@-]+", "_", hint) if hint else f"q{self.seq}"
        layer = f"{self.txn.stmt_id}:{safe}"
        if layer in self.used_layers:
            layer = f"{layer}~{self.seq}"
        self.used_layers.add(layer)
        return layer

    def add_output_node(self, prefix: str, name: str, used: set) -> str:
        node_id = f"{prefix}.{name}"
        n = 2
        while node_id in used:
            node_id = f"{prefix}.{name}#{n}"
            n += 1
        used.add(node_id)
        kind = "ephemeral" if ":" in prefix else "column"
        self.txn.add_node(node_id, kind, prefix, name)
        return node_id

    # ------------------------------------------------------------------ 查询层

    def resolve_query(self, node, scope: Scope | None, prefix: str,
                      out_names: list | None = None) -> list[OutCol]:
        """解析一个查询节点，为其输出字段建节点和边，返回输出字段列表。"""
        if isinstance(node, exp.Subquery):
            return self.resolve_query(node.this, scope, prefix, out_names)
        if isinstance(node, exp.Select):
            return self.resolve_select(node, scope, prefix, out_names)
        if isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
            return self.resolve_setop(node, scope, prefix, out_names)
        if isinstance(node, exp.Values):
            return self.resolve_values(node, prefix, out_names)
        raise UnsupportedConstruct(f"不支持的查询结构: {type(node).__name__}")

    def resolve_values(self, node: exp.Values, prefix: str, out_names) -> list[OutCol]:
        first = node.expressions[0] if node.expressions else None
        width = len(first.expressions) if isinstance(first, exp.Tuple) else 0
        names = out_names or [f"col{i}" for i in range(width)]
        out, used = [], set()
        for name in names:
            nid = self.add_output_node(prefix, name, used)
            out.append(OutCol(name, nid))
        return out

    def resolve_setop(self, node, scope: Scope | None, prefix: str,
                      out_names) -> list[OutCol]:
        is_union = isinstance(node, exp.Union)
        op = type(node).__name__.upper()
        label = (op + ("" if node.args.get("distinct") else " ALL")) if is_union else op
        if node.args.get("by_name"):
            label += " BY NAME"

        left = self.resolve_query(arg(node, "this"), scope, self.new_layer("setL"))
        right = self.resolve_query(arg(node, "expression"), scope, self.new_layer("setR"))
        by_name = bool(node.args.get("by_name"))
        right_by_name = {c.name: c for c in right}
        if not by_name and len(left) != len(right):
            self.txn.warn(
                f"{op} 两侧字段数不一致（左 {len(left)} / 右 {len(right)}），按位置对齐")

        out, used = [], set()
        for i, lc in enumerate(left):
            name = out_names[i] if out_names and i < len(out_names) else lc.name
            if by_name:
                rc = right_by_name.get(lc.name)
                if rc is None:
                    self.txn.warn(f"{op} BY NAME：右侧缺少字段 '{lc.name}'")
            else:
                rc = right[i] if i < len(right) else None
            nid = self.add_output_node(prefix, name, used)
            self.txn.add_edge(lc.node, nid, VALUE, SETOP, label)
            if rc is not None:
                # UNION 两侧都贡献值；EXCEPT/INTERSECT 右侧只起过滤作用
                kind = VALUE if is_union else USAGE
                self.txn.add_edge(rc.node, nid, kind, SETOP, label)
            out.append(OutCol(name, nid))

        # 集合操作上的 ORDER BY 引用的是输出字段名，只应在输出里解析
        out_map = {c.name: c for c in out}
        order = node.args.get("order")
        if order is not None:
            for o in order.expressions:
                label = f"ORDER BY {sql_text(o.this, self.dialect)}"
                for col in o.this.find_all(exp.Column):
                    if isinstance(col.this, exp.Star):
                        continue
                    cname = norm_part(col.this)
                    target = out_map.get(cname)
                    if target is not None:
                        for oc in out:
                            self.txn.add_edge(target.node, oc.node, USAGE,
                                              EXPRESSION, label)
        return out

    def resolve_select(self, select: exp.Select, scope: Scope | None,
                       prefix: str, out_names) -> list[OutCol]:
        with_ = arg(select, "with_", "with")
        if with_ is not None and with_.args.get("recursive"):
            raise UnsupportedConstruct("WITH RECURSIVE 递归 CTE 暂不支持")

        child = Scope(parent=scope)
        if with_ is not None:
            for cte in with_.expressions:
                alias = cte.args.get("alias")
                name = ident_of(alias.this) if alias else ""
                col_override = [ident_of(c) for c in (alias.args.get("columns") or [])] if alias else []
                layer = self.new_layer(f"cte.{name}")
                cols = self.resolve_query(cte.this, child, layer, col_override or None)
                child.ctes[name] = Source(
                    name, layer, "cte", [(c.name, c.node) for c in cols])

        self.build_from(select, child)

        # 逐条解析投影并累积别名表：DuckDB 允许后面的投影/WHERE/HAVING/ORDER BY
        # 引用前面定义的 SELECT 别名
        pouts: list[POut] = []
        alias_map: dict[str, list] = {}
        for proj in select.args.get("expressions") or []:
            new_pouts = self.expand_projection(proj, child, alias_map)
            for p in new_pouts:
                alias_map.setdefault(p.name, p.deps)
            pouts.extend(new_pouts)

        out, used = [], set()
        for idx, pout in enumerate(pouts):
            name = out_names[idx] if out_names and idx < len(out_names) else pout.name
            nid = self.add_output_node(prefix, name, used)
            for dep, ambiguous in pout.deps:
                self.txn.add_edge(dep, nid, VALUE, pout.transform,
                                  pout.expression, ambiguous)
            out.append(OutCol(name, nid))
        if out_names and len(out_names) != len(pouts):
            self.txn.warn(
                f"目标字段数（{len(out_names)}）与 SELECT 输出数（{len(pouts)}）不一致")

        for dep, expr_label in self.collect_usage(select, child, alias_map):
            for oc in out:
                self.txn.add_edge(dep, oc.node, USAGE, EXPRESSION, expr_label)
        return out

    # ------------------------------------------------------------------ FROM

    def build_from(self, select: exp.Select, scope: Scope):
        from_ = arg(select, "from_", "from")
        items = []
        if from_ is not None and from_.this is not None:
            items.append(from_.this)
        for join in select.args.get("joins") or []:
            items.append(join.this)
            for u in join.args.get("using") or []:
                scope.using_cols.add(ident_of(u))
        for item in items:
            scope.add_source(self.make_source(item, scope))

    def lookup_cte(self, name: str, scope: Scope) -> Source | None:
        if "." in name:
            return None
        for sc in scope.chain():
            if name in sc.ctes:
                return sc.ctes[name]
        return None

    def make_source(self, node, scope: Scope) -> Source:
        if isinstance(node, exp.Table):
            if node.args.get("pivots"):
                raise UnsupportedConstruct("PIVOT 暂不支持")
            if not isinstance(node.this, exp.Identifier):
                raise UnsupportedConstruct(f"表函数 {sql_text(node)} 暂不支持")
            name = table_full_name(node)
            alias_node = node.args.get("alias")
            alias = ident_of(alias_node.this) if alias_node else name.rsplit(".", 1)[-1]
            cte = self.lookup_cte(name, scope)
            if cte is not None:  # CTE 名称遮蔽基础表
                return Source(alias, cte.prefix, "cte", list(cte.columns))
            obj = self.schema.get(name)
            if obj is None:
                raise UnknownObject(name)
            cols = [(c["name"], f"{obj.name}.{c['name']}") for c in obj.columns]
            return Source(alias, obj.name, obj.kind, cols)
        if isinstance(node, exp.Subquery):
            inner = node.this
            alias_node = node.args.get("alias")
            alias = ident_of(alias_node.this) if alias_node else f"sub{self.seq + 1}"
            col_override = [ident_of(c) for c in (alias_node.args.get("columns") or [])] if alias_node else []
            layer = self.new_layer(f"sub.{alias}")
            cols = self.resolve_query(inner, scope, layer, col_override or None)
            return Source(alias, layer, "derived", [(c.name, c.node) for c in cols])
        if isinstance(node, exp.Unnest):
            raise UnsupportedConstruct("UNNEST 暂不支持")
        if isinstance(node, exp.Lateral):
            raise UnsupportedConstruct("LATERAL 暂不支持")
        if isinstance(node, exp.Pivot):
            raise UnsupportedConstruct("PIVOT 暂不支持")
        if isinstance(node, exp.Values):
            # FROM (VALUES ...) AS x(a, b)：无上游，字段来自别名或 col0..n
            alias_node = node.args.get("alias")
            alias = ident_of(alias_node.this) if alias_node else f"sub{self.seq + 1}"
            col_override = [ident_of(c) for c in (alias_node.args.get("columns") or [])] if alias_node else []
            layer = self.new_layer(f"sub.{alias}")
            cols = self.resolve_values(node, layer, col_override or None)
            return Source(alias, layer, "derived", [(c.name, c.node) for c in cols])
        raise UnsupportedConstruct(f"不支持的 FROM 项: {type(node).__name__}")

    # ------------------------------------------------------------------ 投影

    def expand_projection(self, proj, scope: Scope, alias_map: dict) -> list[POut]:
        if isinstance(proj, exp.Star):
            return self.expand_star(scope, proj, None, alias_map)
        if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
            qualifier = norm_part(proj.args.get("table")) if proj.args.get("table") else None
            return self.expand_star(scope, proj.this, qualifier, alias_map)

        if isinstance(proj, exp.Alias):
            name = ident_of(proj.args.get("alias"))
            inner = proj.this
        elif isinstance(proj, exp.Column):
            name = ident_of(proj.this)
            inner = proj
        else:
            # 未命名表达式：用表达式文本作字段名（与 DuckDB 的命名一致）
            name = sql_text(proj, self.dialect)
            inner = proj
        deps = self.expr_deps(inner, scope, alias_map=alias_map)
        return [POut(name, deps, self.classify(inner), sql_text(inner, self.dialect))]

    def expand_star(self, scope: Scope, star: exp.Star, qualifier: str | None,
                    alias_map: dict) -> list[POut]:
        if star.args.get("ilike"):
            raise UnsupportedConstruct("SELECT * ILIKE 暂不支持")
        except_names = {ident_of(c) for c in (star.args.get("except_") or [])}
        replacements = {}
        for rep in star.args.get("replace") or []:
            if isinstance(rep, exp.Alias):
                replacements[ident_of(rep.args.get("alias"))] = rep.this
        renames = {}
        for ren in star.args.get("rename") or []:
            if isinstance(ren, exp.Alias):
                renames[ident_of(ren.this)] = ident_of(ren.args.get("alias"))

        sources = scope.sources
        if qualifier:
            src = scope.by_alias.get(qualifier)
            if src is None:
                self.txn.warn(f"星号限定符 '{qualifier}' 不在 FROM 来源中")
                return []
            sources = [src]
        if not sources:
            self.txn.warn("SELECT * 没有可展开的来源（FROM 为空或来源未知）")
            return []

        outs = []
        for src in sources:
            for cname, node_id in src.columns:
                if cname in except_names:
                    continue
                if cname in replacements:
                    expr = replacements[cname]
                    deps = self.expr_deps(expr, scope, alias_map=alias_map)
                    outs.append(POut(cname, deps, self.classify(expr),
                                     sql_text(expr, self.dialect)))
                    continue
                outs.append(POut(renames.get(cname, cname),
                                 [(node_id, False)], DIRECT, cname))
        return outs

    # ------------------------------------------------------------------ 表达式依赖

    def expr_deps(self, node, scope: Scope, bound=frozenset(),
                  alias_map: dict | None = None) -> list:
        """收集表达式引用的字段节点；进入子查询时递归建层。"""
        deps = []

        def visit(n, bound_names):
            if n is None:
                return
            if isinstance(n, exp.Lambda):
                params = {norm_part(p) for p in n.expressions}
                visit(n.this, bound_names | params)
                return
            if isinstance(n, exp.Column):
                if isinstance(n.this, exp.Star):
                    return  # count(*) 等
                deps.extend(self.resolve_column(n, scope, bound_names, alias_map))
                return
            if isinstance(n, exp.Star):
                return
            if isinstance(n, exp.Subquery):
                inner = n.this
                if isinstance(inner, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
                    cols = self.resolve_query(inner, scope, self.new_layer("subq"))
                    deps.extend((c.node, False) for c in cols)
                return
            if isinstance(n, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
                # EXISTS / IN 子查询（未包在 Subquery 节点里）
                cols = self.resolve_query(n, scope, self.new_layer("subq"))
                deps.extend((c.node, False) for c in cols)
                return
            for value in n.args.values():
                if isinstance(value, exp.Expression):
                    visit(value, bound_names)
                elif isinstance(value, list):
                    for v in value:
                        if isinstance(v, exp.Expression):
                            visit(v, bound_names)

        visit(node, bound)
        return deps

    def resolve_column(self, col: exp.Column, scope: Scope, bound=frozenset(),
                       alias_map: dict | None = None) -> list:
        parts = [norm_part(p) for p in col.parts if not isinstance(p, exp.Star)]
        if not parts:
            return []
        if len(parts) == 1:
            name = parts[0]
            if name in bound:
                return []  # lambda 参数，不是字段引用
            found = self._resolve_unqualified(name, scope)
            if found is not None:
                return found
            if alias_map and name in alias_map:
                # SELECT 别名兜底（DuckDB 允许在投影/WHERE/HAVING/ORDER BY 中引用别名）
                return alias_map[name]
            self.txn.warn(f"无法解析的字段引用 '{name}'")
            return []

        qualifier, name = parts[-2], parts[-1]
        for sc in scope.chain():
            src = sc.by_alias.get(qualifier)
            if src is not None:
                node_id = src.column_map().get(name)
                if node_id is not None:
                    return [(node_id, False)]
                self.txn.warn(f"来源 '{qualifier}' 中不存在字段 '{name}'")
                return []
        self.txn.warn(f"无法解析的限定符 '{qualifier}'（字段 {'.'.join(parts)}）")
        return []

    def _resolve_unqualified(self, name: str, scope: Scope):
        """在作用域链中解析无限定字段名；找到返回依赖列表，找不到返回 None。"""
        for sc in scope.chain():
            cands = [s for s in sc.sources if name in s.column_map()]
            if len(cands) == 1:
                return [(cands[0].column_map()[name], False)]
            if len(cands) > 1:
                if name in sc.using_cols:
                    # JOIN USING 的合并字段：值来自两侧，依赖两边但不算歧义
                    return [(s.column_map()[name], False) for s in cands]
                names = "、".join(f"'{s.alias}.{name}'" for s in cands)
                self.txn.warn(f"字段引用 '{name}' 在多个来源中同名（{names}），已按歧义处理")
                return [(s.column_map()[name], True) for s in cands]
        return None

    def classify(self, expr) -> str:
        if isinstance(expr, exp.Column):
            return DIRECT
        found_window = found_agg = False
        for n in expr.walk():
            if isinstance(n, exp.Window):
                found_window = True
            elif isinstance(n, exp.AggFunc):
                found_agg = True
        if found_window:
            return WINDOW
        if found_agg:
            return AGGREGATE
        return EXPRESSION

    # ------------------------------------------------------------------ 过滤/分组等引用

    def collect_usage(self, select: exp.Select, scope: Scope, alias_map: dict) -> list:
        """WHERE/GROUP BY/HAVING/QUALIFY/ORDER BY/JOIN ON 中的字段引用。

        这些字段不产生输出值，但删除或改名会让语句失败，影响分析必须覆盖。
        """
        out = []

        def add(node, label):
            if node is None:
                return
            for dep, _ambiguous in self.expr_deps(node, scope, alias_map=alias_map):
                out.append((dep, label))

        where = select.args.get("where")
        if where:
            add(where.this, f"WHERE {sql_text(where.this, self.dialect)}")
        group = select.args.get("group")
        if group:
            for g in group.expressions:
                add(g, f"GROUP BY {sql_text(g, self.dialect)}")
        having = select.args.get("having")
        if having:
            add(having.this, f"HAVING {sql_text(having.this, self.dialect)}")
        qualify = select.args.get("qualify")
        if qualify:
            add(qualify.this, f"QUALIFY {sql_text(qualify.this, self.dialect)}")
        out.extend(self.order_deps(select, scope, alias_map))
        for w in select.args.get("windows") or []:
            for p in w.args.get("partition_by") or []:
                add(p, f"WINDOW PARTITION BY {sql_text(p, self.dialect)}")
            order = w.args.get("order")
            if order:
                for o in order.expressions:
                    add(o.this, f"WINDOW ORDER BY {sql_text(o.this, self.dialect)}")
        for j in select.args.get("joins") or []:
            on = j.args.get("on")
            if on is not None:
                add(on, f"JOIN ON {sql_text(on, self.dialect)}")
            for u in j.args.get("using") or []:
                uname = ident_of(u)
                found = self._resolve_unqualified(uname, scope)
                if found is None:
                    self.txn.warn(f"无法解析的 JOIN USING 字段 '{uname}'")
                else:
                    for dep, _ in found:
                        out.append((dep, f"JOIN USING ({uname})"))
        return out

    def order_deps(self, node, scope: Scope | None, alias_map: dict | None) -> list:
        out = []
        order = node.args.get("order") if node is not None else None
        if order is None or scope is None:
            return out
        for o in order.expressions:
            label = f"ORDER BY {sql_text(o.this, self.dialect)}"
            for dep, _ in self.expr_deps(o.this, scope, alias_map=alias_map):
                out.append((dep, label))
        return out


# ---------------------------------------------------------------------- 语句级


def _create_target(ast: exp.Create):
    """返回 (对象名, 字段名覆盖列表)。"""
    this = ast.this
    if isinstance(this, exp.Schema):
        table = this.this
        cols = [ident_of(c) for c in this.expressions
                if isinstance(c, (exp.Identifier, exp.Column))]
        return table_full_name(table), cols
    if isinstance(this, exp.Table):
        return table_full_name(this), []
    return "", []


def _columns_from_schema(ast: exp.Create) -> list:
    cols = []
    this = ast.this
    if not isinstance(this, exp.Schema):
        return cols
    for cd in this.expressions:
        if isinstance(cd, exp.ColumnDef):
            kind = cd.args.get("kind")
            cols.append({
                "name": ident_of(cd.this),
                "type": kind.sql(dialect=DIALECT) if kind else "",
            })
    return cols


def resolve_statement(ast, schema: SchemaRegistry, txn: Txn, dialect: str = DIALECT) -> dict:
    """解析单条语句，结果写入 txn。返回 {'kind':..., 'target':...}。

    可能抛出 UnknownObject（可稍后重试）或 UnsupportedConstruct（不可重试）。
    """
    resolver = QueryResolver(schema, txn, dialect)

    if isinstance(ast, exp.Create):
        kind = (ast.args.get("kind") or "").upper()
        query = ast.args.get("expression")
        if kind == "TABLE":
            name, _ = _create_target(ast)
            if not name:
                raise UnsupportedConstruct("无法识别的 CREATE TABLE 目标")
            if query is not None:  # CREATE TABLE AS SELECT
                cols = resolver.resolve_query(query, None, name)
                txn.registrations.append(ObjectDef(
                    name=name, kind="table",
                    columns=[{"name": c.name, "type": "UNKNOWN"} for c in cols],
                    source="sql", defined_by=dict(txn.location)))
                return {"kind": "ctas", "target": name}
            cols = _columns_from_schema(ast)
            if not cols:
                raise UnsupportedConstruct("CREATE TABLE 缺少字段定义或查询")
            txn.registrations.append(ObjectDef(
                name=name, kind="table", columns=cols,
                source="sql", defined_by=dict(txn.location)))
            return {"kind": "create_table", "target": name}
        if kind == "VIEW":
            name, col_override = _create_target(ast)
            if not name or query is None:
                raise UnsupportedConstruct("无法识别的 CREATE VIEW")
            cols = resolver.resolve_query(query, None, name, col_override or None)
            txn.registrations.append(ObjectDef(
                name=name, kind="view",
                columns=[{"name": c.name, "type": "UNKNOWN"} for c in cols],
                source="sql", defined_by=dict(txn.location)))
            return {"kind": "create_view", "target": name}
        raise UnsupportedConstruct(f"不支持的 CREATE 类型: {kind or '未知'}")

    if isinstance(ast, exp.Insert):
        return _resolve_insert(ast, resolver, schema, txn)

    if isinstance(ast, exp.Drop):
        for t in ast.args.get("tables") or []:
            txn.unregistrations.append(table_full_name(t))
        return {"kind": "drop", "target": ""}

    if isinstance(ast, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        # 裸查询：没有持久目标，把输出放进临时层，仍记录它读取了哪些字段
        layer = resolver.new_layer("q")
        resolver.resolve_query(ast, None, layer)
        return {"kind": "select", "target": ""}

    raise UnsupportedConstruct(f"不支持的语句类型: {type(ast).__name__}")


def _resolve_insert(ast: exp.Insert, resolver: QueryResolver,
                    schema: SchemaRegistry, txn: Txn) -> dict:
    target = ast.this
    col_list: list[str] = []
    if isinstance(target, exp.Schema):
        tname = table_full_name(target.this)
        col_list = [ident_of(c) for c in target.expressions]
    elif isinstance(target, exp.Table):
        tname = table_full_name(target)
    else:
        raise UnsupportedConstruct("无法识别的 INSERT 目标")

    query = ast.args.get("expression")
    if isinstance(query, exp.Values):
        return {"kind": "insert", "target": tname}  # 字面量 VALUES 无上游
    if not isinstance(query, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        raise UnsupportedConstruct(
            f"INSERT 的来源暂不支持: {type(query).__name__ if query is not None else '空'}")

    layer = resolver.new_layer("ins")
    cols = resolver.resolve_query(query, None, layer)

    obj = schema.get(tname)
    by_name = bool(ast.args.get("by_name"))
    mapping: list[tuple[str, OutCol]] = []  # (目标字段名, 源输出)
    if col_list:
        if len(col_list) != len(cols):
            txn.warn(f"INSERT 字段列表数（{len(col_list)}）与 SELECT 输出数（{len(cols)}）不一致")
        mapping = list(zip(col_list, cols))
    elif by_name:
        if obj is None:
            txn.warn(f"INSERT 目标表 '{tname}' 不在 schema 中，BY NAME 无法对齐")
        else:
            for c in cols:
                if c.name in obj.column_names():
                    mapping.append((c.name, c))
                else:
                    txn.warn(f"BY NAME：目标表 '{tname}' 中不存在字段 '{c.name}'")
    elif obj is not None:
        names = obj.column_names()
        if len(names) != len(cols):
            txn.warn(f"INSERT 目标表 '{tname}' 字段数（{len(names)}）与 SELECT 输出数（{len(cols)}）不一致，按位置对齐")
        mapping = list(zip(names, cols))
    else:
        txn.warn(f"INSERT 目标表 '{tname}' 不在 schema 中，按 SELECT 输出名记录")
        mapping = [(c.name, c) for c in cols]

    used: set = set()
    for tcol, src in mapping:
        nid = resolver.add_output_node(tname, tcol, used)
        txn.add_edge(src.node, nid, VALUE, DIRECT, f"INSERT INTO {tname}")
    return {"kind": "insert", "target": tname}
