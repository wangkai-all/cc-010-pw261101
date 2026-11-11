"""CLI 端到端测试。"""
import json
import os

from sql_lineage.cli import main

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def test_analyze_and_impact_cli(tmp_path):
    lineage_json = str(tmp_path / "lineage.json")
    lineage_html = str(tmp_path / "lineage.html")
    rc = main([
        "analyze",
        "--schema", os.path.join(EXAMPLES, "schema.json"),
        "--sql", os.path.join(EXAMPLES, "sql"),
        "--json", lineage_json,
        "--html", lineage_html,
    ])
    assert rc == 0
    with open(lineage_json, encoding="utf-8") as f:
        report = json.load(f)
    assert report["stats"]["unresolved"] == 6
    assert report["stats"]["cycles"] == 1
    html = open(lineage_html, encoding="utf-8").read()
    assert "血缘浏览" in html
    assert "v_customer_360" in html
    assert "cyc_a" in html  # 循环依赖展示

    impact_json = str(tmp_path / "impact.json")
    impact_html = str(tmp_path / "impact.html")
    rc = main([
        "impact",
        "--lineage", lineage_json,
        "--changes", os.path.join(EXAMPLES, "changes.json"),
        "--json", impact_json,
        "--html", impact_html,
    ])
    assert rc == 0
    with open(impact_json, encoding="utf-8") as f:
        impact = json.load(f)
    assert len(impact["changes"]) == 3
    html = open(impact_html, encoding="utf-8").read()
    assert "影响分析" in html
    assert "customer_name" in html


def test_analyze_missing_sql(tmp_path, capsys):
    rc = main(["analyze", "--sql", str(tmp_path / "empty_dir")])
    assert rc == 2
