"""变更影响分析测试。"""
import json
import os

import pytest

from sql_lineage import Analyzer, compute_impact

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


@pytest.fixture(scope="module")
def impact():
    an = Analyzer()
    an.load_manifest_file(os.path.join(EXAMPLES, "schema.json"))
    sql_dir = os.path.join(EXAMPLES, "sql")
    for name in sorted(os.listdir(sql_dir)):
        if name.endswith(".sql"):
            an.add_sql_file(os.path.join(sql_dir, name))
    report = an.run()
    with open(os.path.join(EXAMPLES, "changes.json"), encoding="utf-8") as f:
        changes = json.load(f)["changes"]
    return compute_impact(report, changes)


def affected_objects(change):
    return {a["object"] for a in change["affected"] if not a.get("truncated")}


def test_drop_column_impact(impact):
    drop = next(c for c in impact["changes"] if c["change"] == "drop")
    assert drop["found"] is True
    objs = affected_objects(drop)
    # raw_orders.status 的下游：stg_orders → v_order_clean；
    # v_customer_360 的 CTE（WHERE 引用）、v_region_daily 第二分支 → daily_metrics / mart
    assert {"stg_orders", "v_order_clean", "v_customer_360",
            "v_region_daily", "daily_metrics", "mart_customer_ltv"} <= objs
    # 未解析的 v_ghost 语句引用了 raw_orders，应提示人工核对
    assert any("v_ghost" in cav or "人工核对" in cav for cav in drop["caveats"])


def test_drop_impact_includes_usage_edges(impact):
    """删除字段的影响必须覆盖 WHERE/JOIN 等引用位置，不只是投影。"""
    drop = next(c for c in impact["changes"] if c["change"] == "drop")
    usage_hits = [a for a in drop["affected"]
                  if not a.get("truncated") and a["edge_kind"] == "usage"]
    assert usage_hits, "应存在经 usage 边传播的影响"


def test_rename_impact(impact):
    ren = next(c for c in impact["changes"] if c["change"] == "rename")
    assert ren["new_name"] == "customer_name"
    objs = affected_objects(ren)
    assert {"v_customer_360", "mart_customer_ltv"} <= objs
    cols = {a["column"] for a in ren["affected"] if not a.get("truncated")}
    assert "full_name" in cols


def test_type_change_keeps_expression(impact):
    tc = next(c for c in impact["changes"] if c["change"] == "type")
    assert tc["new_type"] == "DECIMAL(18,2)"
    objs = affected_objects(tc)
    assert {"stg_orders", "v_order_clean", "v_customer_360",
            "v_region_daily", "daily_metrics", "mart_customer_ltv"} <= objs
    # 直接引用 raw_orders.amount 的表达式要保留，方便核对类型敏感性
    exprs = [a["direct_expression"] for a in tc["affected"] if not a.get("truncated")]
    assert any("CAST" in e for e in exprs)


def test_impact_path_recorded(impact):
    drop = next(c for c in impact["changes"] if c["change"] == "drop")
    sample = next(a for a in drop["affected"]
                  if not a.get("truncated") and a["object"] == "v_customer_360")
    assert sample["via"][0] == "raw_orders.status"
    assert sample["via"][-1].startswith("v_customer_360.")
    assert len(sample["via"]) >= 3, "应记录经过中间层的完整路径"


def test_unknown_change_not_found(impact):
    from sql_lineage import Analyzer as _A  # noqa
    report = {"nodes": {}, "objects": {}, "edges": [], "statements": []}
    res = compute_impact(report, [{"table": "nope", "column": "x", "change": "drop"}])
    assert res["changes"][0]["found"] is False
    assert res["changes"][0]["caveats"]


def test_impact_json_serializable(impact):
    json.dumps(impact)
