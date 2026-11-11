"""命令行入口：

  python -m sql_lineage analyze --schema schema.json --sql examples/sql \
      --json out/lineage.json --html out/lineage.html
  python -m sql_lineage impact --lineage out/lineage.json --changes changes.json \
      --json out/impact.json --html out/impact.html
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .analyzer import Analyzer
from .impact import CHANGE_LABELS, compute_impact
from .web import render_html


def _collect_sql_files(paths: list) -> list:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith(".sql"):
                    files.append(os.path.join(p, name))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"路径不存在，已跳过: {p}", file=sys.stderr)
    return files


def _write(path: str, content: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def cmd_analyze(args) -> int:
    analyzer = Analyzer(dialect=args.dialect)
    if args.schema:
        analyzer.load_manifest_file(args.schema)
    sql_files = _collect_sql_files(args.sql)
    if not sql_files:
        print("未找到 SQL 文件", file=sys.stderr)
        return 2
    for path in sql_files:
        analyzer.add_sql_file(path)
    report = analyzer.run()

    stats = report["stats"]
    print(f"分析完成：对象 {stats['objects']}，血缘边 {stats['edges']}，"
          f"语句 {stats['statements']}（正常 {stats['ok']} / 部分 {stats['partial']} / "
          f"未解析 {stats['unresolved']}），循环依赖 {stats['cycles']} 处")
    if not stats["complete"]:
        print("⚠ 血缘不完整：存在未解析/部分解析的语句"
              + ("、循环依赖" if stats["cycles"] else "")
              + "，影响分析结果可能遗漏", file=sys.stderr)
        for s in report["statements"]:
            if s["status"] == "unresolved":
                print(f"  [未解析] {s['id']} {s['file']}:{s['line']} — "
                      + "; ".join(s["reasons"]), file=sys.stderr)
        for cyc in report["cycles"]:
            print("  [循环] " + " → ".join(cyc["path"]), file=sys.stderr)

    if args.json:
        _write(args.json, json.dumps(report, ensure_ascii=False, indent=2))
        print(f"JSON 报告: {args.json}")
    if args.html:
        _write(args.html, render_html(report))
        print(f"网页报告: {args.html}")
    if not args.json and not args.html:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    return 0


def cmd_impact(args) -> int:
    with open(args.lineage, "r", encoding="utf-8") as f:
        report = json.load(f)
    with open(args.changes, "r", encoding="utf-8") as f:
        changes = json.load(f).get("changes", [])
    impact = compute_impact(report, changes)

    for ch in impact["changes"]:
        label = ch["change_label"] or ch["change"]
        extra = f" → {ch['new_name']}" if ch.get("new_name") else \
                (f" → {ch['new_type']}" if ch.get("new_type") else "")
        n = len([a for a in ch["affected"] if not a.get("truncated")])
        print(f"[{label}] {ch['table']}.{ch['column']}{extra}：影响 {n} 个下游位置")
        if not ch["found"]:
            print("  ⚠ 该字段不在已知血缘中")
        for a in ch["affected"]:
            if a.get("truncated"):
                print(f"  ⚠ {a['note']}")
                continue
            print(f"  - {a['object']}.{a['column']} "
                  f"({'引用' if a['edge_kind'] == 'usage' else '值'}，{a['transform']}，"
                  f"{a['location'].get('file', '')}:{a['location'].get('line', '')})")
        for cav in ch["caveats"]:
            print(f"  ⚠ {cav}")

    if args.json:
        _write(args.json, json.dumps(impact, ensure_ascii=False, indent=2))
        print(f"JSON 报告: {args.json}")
    if args.html:
        _write(args.html, render_html(report, impact))
        print(f"网页报告: {args.html}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="sql_lineage",
        description="DuckDB 方言的字段级 SQL 血缘分析工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("analyze", help="解析 schema 清单 + SQL，生成血缘报告")
    pa.add_argument("--schema", help="表结构清单 JSON（可选）")
    pa.add_argument("--sql", nargs="+", required=True, help="SQL 文件或目录（目录按文件名排序处理）")
    pa.add_argument("--dialect", default="duckdb", help="SQL 方言（默认 duckdb）")
    pa.add_argument("--json", help="血缘 JSON 输出路径")
    pa.add_argument("--html", help="网页报告输出路径")
    pa.set_defaults(func=cmd_analyze)

    pi = sub.add_parser("impact", help="基于血缘报告做表结构变更影响分析")
    pi.add_argument("--lineage", required=True, help="analyze 生成的血缘 JSON")
    pi.add_argument("--changes", required=True, help="变更清单 JSON")
    pi.add_argument("--json", help="影响分析 JSON 输出路径")
    pi.add_argument("--html", help="含影响分析的网页报告输出路径")
    pi.set_defaults(func=cmd_impact)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
