"""基于 examples/ 的端到端测试 + 针对解析器的单元测试。"""
import json
import os

import pytest

from sql_lineage import Analyzer

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


@pytest.fixture(scope="module")
def report():
    an = Analyzer()
    an.load_manifest_file(os.path.join(EXAMPLES, "schema.json"))
    sql_dir = os.path.join(EXAMPLES, "sql")
    for name in sorted(os.listdir(sql_dir)):
        if name.endswith(".sql"):
            an.add_sql_file(os.path.join(sql_dir, name))
    return an.run()


def upstream_nodes(report, start, max_depth=30):
    """从某字段节点向上游遍历，返回可达节点集合。"""
    in_edges = {}
    for e in report["edges"]:
        in_edges.setdefault(e["dst"], []).append(e["src"])
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(in_edges.get(node, []))
    return seen


def downstream_nodes(report, start):
    out_edges = {}
    for e in report["edges"]:
        out_edges.setdefault(e["src"], []).append(e["dst"])
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(out_edges.get(node, []))
    return seen


# ------------------------------------------------------------------ 对象与结构

def test_objects_registered(report):
    objs = report["objects"]
    for name in ["raw_customers", "raw_orders", "raw_events",
                 "stg_orders", "daily_metrics",
                 "v_order_clean", "v_customer_360", "v_region_daily",
                 "mart_customer_ltv"]:
        assert name in objs, f"缺少对象 {name}"
    assert objs["v_order_clean"]["kind"] == "view"
    assert objs["stg_orders"]["columns"][0] == {"name": "order_id", "type": "BIGINT"}


def test_star_expand_with_exclude(report):
    cols = report["objects"]["v_order_clean"]["columns"]
    names = [c["name"] for c in cols]
    # stg_orders 去掉 note，再加 tax / join_hint
    assert "note" not in names
    for c in ["order_id", "customer_id", "amount", "quantity",
              "status", "ordered_at", "tax", "join_hint"]:
        assert c in names


def test_string_literal_not_lineage(report):
    """字符串字面量里的表名不能产生血缘。"""
    for e in report["edges"]:
        assert not (e["src"].startswith("raw_customers.")
                    and e["dst"].split(".")[0] in ("stg_orders", "v_order_clean")), e
    # note / join_hint 两个常量列的上游不应触及任何 raw_* 基础表
    for start in ["stg_orders.note", "v_order_clean.join_hint"]:
        ups = upstream_nodes(report, start)
        assert not any(n.startswith("raw_") for n in ups), (start, ups)


def test_cte_shadowing_base_table(report):
    """v_customer_360 里的 CTE raw_orders 遮蔽基础表：per_customer 的字段
    应来自 CTE 的 4 个字段，而不是基础表的全部 6 个。"""
    cte_cols = sorted(nid for nid in report["nodes"]
                      if ":cte.raw_orders." in nid)
    assert len(cte_cols) == 4
    assert any(nid.endswith(".amount") for nid in cte_cols)
    assert not any(nid.endswith(".status") for nid in cte_cols)
    # CTE 的 WHERE 引用了基础表的 status（usage 边）
    usage = [e for e in report["edges"]
             if e["src"] == "raw_orders.status" and e["kind"] == "usage"
             and ":cte.raw_orders." in e["dst"]]
    assert usage, "CTE 的 WHERE 应对基础表 status 产生 usage 边"


def test_window_and_aggregate_transforms(report):
    edges = report["edges"]
    # 窗口函数标记在 ranked CTE 层的输出边上
    win = [e for e in edges if e["dst"].endswith(":cte.ranked.amount_rank")
           and e["kind"] == "value"]
    assert win and all(e["transform"] == "window" for e in win)
    agg = [e for e in edges if e["transform"] == "aggregate"
           and "per_customer.total_amount" in e["dst"]]
    assert agg, "sum(amount) 应标记为聚合"


def test_expression_preserved(report):
    """计算字段要保留表达式文本，方便核对。"""
    tax = [e for e in report["edges"] if e["dst"] == "v_order_clean.tax"]
    assert tax and "0.13" in tax[0]["expression"]
    assert tax[0]["location"]["file"].endswith("02_views.sql")
    assert tax[0]["location"]["line"] > 0


def test_union_alias_reuse(report):
    """UNION ALL 两个分支复用别名 o/c，revenue 应同时上溯到
    stg_orders.amount 和 raw_orders.amount。"""
    ups = upstream_nodes(report, "v_region_daily.revenue")
    assert "stg_orders.amount" in ups
    assert "raw_orders.amount" in ups
    # 两个分支的 join 都引用 raw_customers.customer_id
    assert "raw_customers.customer_id" in ups


def test_multi_layer_path(report):
    """raw_orders.amount → CTE → 聚合 → 窗口层 → 视图 → CTAS 的完整链路。"""
    downs = downstream_nodes(report, "raw_orders.amount")
    assert "v_customer_360.total_amount" in downs
    assert "mart_customer_ltv.total_amount" in downs
    assert "daily_metrics.revenue" in downs
    # 中间路径包含临时层节点
    assert any(":cte.raw_orders.amount" in n for n in downs)
    assert any(":cte.per_customer.total_amount" in n for n in downs)


def test_insert_by_name(report):
    ups = upstream_nodes(report, "daily_metrics.order_cnt")
    assert "v_region_daily.order_cnt" in ups


def test_insert_column_list_mapping(report):
    """INSERT 显式字段列表：stg_orders.amount 来自计算表达式。"""
    edges = [e for e in report["edges"] if e["dst"] == "stg_orders.amount"]
    assert edges
    ins_edges = [e for e in report["edges"]
                 if e["dst"].startswith("s") and ":ins.amount" in e["dst"]]
    srcs = {e["src"] for e in ins_edges}
    assert "raw_orders.amount" in srcs and "raw_orders.quantity" in srcs


# ------------------------------------------------------------------ 未解析与循环

def test_unresolved_statements(report):
    unresolved = {s["id"]: s for s in report["statements"] if s["status"] == "unresolved"}
    # 04_broken.sql 4 条 + 05_cycle.sql 2 条
    assert len(unresolved) == 6
    reasons = " ".join(r for s in unresolved.values() for r in s["reasons"])
    assert "解析失败" in reasons
    assert "递归" in reasons
    assert "PIVOT" in reasons
    assert "未定义的对象" in reasons
    assert report["stats"]["complete"] is False


def test_cycle_detected(report):
    assert len(report["cycles"]) == 1
    path = report["cycles"][0]["path"]
    assert set(path) == {"cyc_a", "cyc_b"}
    assert path[0] == path[-1], "循环路径应首尾相接"
    cyc_stmts = [s for s in report["statements"]
                 if s["target"] in ("cyc_a", "cyc_b") or "cyc" in s["summary"]]
    assert all(s["status"] == "unresolved" for s in cyc_stmts)
    assert any("循环依赖" in r for s in cyc_stmts for r in s["reasons"])


def test_unresolved_refs_for_impact(report):
    """未解析语句的 refs 要保留，供影响分析提示人工核对。"""
    ghost = [s for s in report["statements"] if "v_ghost" in s["summary"]]
    assert ghost and "raw_orders" in ghost[0]["refs"]


# ------------------------------------------------------------------ 单元场景

def analyze(sql, manifest=None):
    an = Analyzer()
    an.load_manifest(manifest or {"tables": {}})
    an.add_sql_text(sql, filename="<test>")
    return an.run()


def test_ambiguous_unqualified_column():
    rep = analyze(
        "CREATE VIEW v AS SELECT id FROM a JOIN b ON a.x = b.x;",
        {"tables": {"a": {"columns": [{"name": "id"}, {"name": "x"}]},
                    "b": {"columns": [{"name": "id"}, {"name": "x"}]}}})
    st = rep["statements"][0]
    assert st["status"] == "partial"
    assert any("歧义" in w["msg"] for w in st["warnings"])
    amb = [e for e in rep["edges"] if e["dst"] == "v.id" and e.get("ambiguous")]
    assert {e["src"] for e in amb} == {"a.id", "b.id"}


def test_unknown_column_warns():
    rep = analyze("CREATE VIEW v AS SELECT nope FROM a;",
                  {"tables": {"a": {"columns": [{"name": "id"}]}}})
    st = rep["statements"][0]
    assert st["status"] == "partial"
    assert any("nope" in w["msg"] for w in st["warnings"])


def test_forward_reference_multipass():
    """视图在使用之后才定义：多趟解析应能处理。"""
    rep = analyze(
        "CREATE VIEW v1 AS SELECT x FROM v2;\n"
        "CREATE VIEW v2 AS SELECT x FROM t;\n",
        {"tables": {"t": {"columns": [{"name": "x"}]}}})
    assert all(s["status"] == "ok" for s in rep["statements"])
    assert "v2.x" in upstream_nodes(rep, "v1.x")
    assert "t.x" in upstream_nodes(rep, "v1.x")


def test_union_by_name():
    rep = analyze(
        "CREATE VIEW v AS SELECT a, b FROM t1 UNION ALL BY NAME SELECT b, a FROM t2;",
        {"tables": {"t1": {"columns": [{"name": "a"}, {"name": "b"}]},
                    "t2": {"columns": [{"name": "a"}, {"name": "b"}]}}})
    ups = upstream_nodes(rep, "v.a")
    assert {"t1.a", "t2.a"} <= ups


def test_except_right_side_is_usage():
    rep = analyze(
        "CREATE VIEW v AS SELECT a FROM t1 EXCEPT SELECT a FROM t2;",
        {"tables": {"t1": {"columns": [{"name": "a"}]},
                    "t2": {"columns": [{"name": "a"}]}}})
    right = [e for e in rep["edges"] if e["dst"] == "v.a" and ":setR" in e["src"]]
    assert right and all(e["kind"] == "usage" for e in right)


def test_view_column_list_rename():
    rep = analyze("CREATE VIEW v(p, q) AS SELECT a, b FROM t;",
                  {"tables": {"t": {"columns": [{"name": "a"}, {"name": "b"}]}}})
    assert [c["name"] for c in rep["objects"]["v"]["columns"]] == ["p", "q"]
    assert "t.a" in upstream_nodes(rep, "v.p")


def test_lambda_param_not_column():
    rep = analyze(
        "CREATE VIEW v AS SELECT list_transform(tags, x -> x || sfx) AS t FROM a;",
        {"tables": {"a": {"columns": [{"name": "tags"}, {"name": "sfx"}]}}})
    st = rep["statements"][0]
    assert st["status"] == "ok", st["warnings"]
    ups = upstream_nodes(rep, "v.t")
    assert {"a.tags", "a.sfx"} <= ups


def test_select_alias_in_where():
    """DuckDB 允许 WHERE 引用 SELECT 别名。"""
    rep = analyze(
        "CREATE VIEW v AS SELECT a * 2 AS d FROM t WHERE d > 10;",
        {"tables": {"t": {"columns": [{"name": "a"}]}}})
    st = rep["statements"][0]
    assert st["status"] == "ok", st["warnings"]
    usage = [e for e in rep["edges"] if e["dst"] == "v.d" and e["kind"] == "usage"]
    assert usage and any(":ins" not in e["src"] for e in usage)


def test_values_insert_no_edges():
    rep = analyze("INSERT INTO t VALUES (1, 2);",
                  {"tables": {"t": {"columns": [{"name": "a"}, {"name": "b"}]}}})
    assert rep["statements"][0]["status"] == "ok"
    assert rep["edges"] == []


def test_ignored_statements():
    rep = analyze("SET threads = 4;\nCREATE VIEW v AS SELECT a FROM t;",
                  {"tables": {"t": {"columns": [{"name": "a"}]}}})
    statuses = [s["status"] for s in rep["statements"]]
    assert statuses == ["ignored", "ok"]


def test_star_replace_and_rename():
    rep = analyze(
        "CREATE VIEW v AS SELECT * REPLACE (a + 1 AS a) RENAME (b AS c) FROM t;",
        {"tables": {"t": {"columns": [{"name": "a"}, {"name": "b"}]}}})
    names = [c["name"] for c in rep["objects"]["v"]["columns"]]
    assert names == ["a", "c"]
    assert rep["statements"][0]["status"] == "ok", rep["statements"][0]["warnings"]


def test_cte_column_alias_list():
    rep = analyze(
        "CREATE VIEW v AS WITH c(p, q) AS (SELECT a, b FROM t) SELECT p, q FROM c;",
        {"tables": {"t": {"columns": [{"name": "a"}, {"name": "b"}]}}})
    assert [c["name"] for c in rep["objects"]["v"]["columns"]] == ["p", "q"]
    assert "t.a" in upstream_nodes(rep, "v.p")


def test_drop_table_unregister():
    rep = analyze(
        "CREATE VIEW v AS SELECT a FROM t;\nDROP TABLE t;\n",
        {"tables": {"t": {"columns": [{"name": "a"}]}}})
    assert "t" not in rep["objects"]


def test_self_insert_cycle_safe():
    """INSERT INTO t SELECT FROM t 会在图中成环，遍历不能死循环。"""
    from sql_lineage import compute_impact
    rep = analyze("INSERT INTO t SELECT a, b FROM t;",
                  {"tables": {"t": {"columns": [{"name": "a"}, {"name": "b"}]}}})
    assert rep["statements"][0]["status"] == "ok"
    imp = compute_impact(rep, [{"table": "t", "column": "a", "change": "drop"}])
    assert imp["changes"][0]["found"] is True  # 不死循环、正常返回


def test_join_using_not_ambiguous():
    """JOIN USING 的合并字段在两侧同名是正常语义，不应报歧义。"""
    rep = analyze(
        "CREATE VIEW v AS SELECT id, x, y FROM t1 JOIN t2 USING (id);",
        {"tables": {"t1": {"columns": [{"name": "id"}, {"name": "x"}]},
                    "t2": {"columns": [{"name": "id"}, {"name": "y"}]}}})
    st = rep["statements"][0]
    assert st["status"] == "ok", st["warnings"]
    # 合并字段的值可能来自任一侧：两边都应有值边，且不标歧义
    ups = [e for e in rep["edges"] if e["dst"] == "v.id" and e["kind"] == "value"]
    assert {e["src"] for e in ups} == {"t1.id", "t2.id"}
    assert not any(e.get("ambiguous") for e in ups)


def test_values_in_from():
    rep = analyze(
        "CREATE VIEW v AS SELECT a, b FROM (VALUES (1, 2)) AS x(a, b);")
    assert rep["statements"][0]["status"] == "ok"
    assert [c["name"] for c in rep["objects"]["v"]["columns"]] == ["a", "b"]


def test_show_ignored():
    rep = analyze("SHOW TABLES;\nCREATE VIEW v AS SELECT a FROM t;",
                  {"tables": {"t": {"columns": [{"name": "a"}]}}})
    assert [s["status"] for s in rep["statements"]] == ["ignored", "ok"]


def test_report_json_serializable(report):
    json.dumps(report)  # 不应抛异常
