"""表结构变更影响分析：给定删除/改名/改类型清单，找出受影响的下游位置。

遍历血缘图中该字段的下游（value 与 usage 两类边都参与），
报告每个受影响的持久字段及其到达路径、表达式和来源位置。
未解析语句里引用了被改表的，单独列为"需人工核对"，因为血缘不完整。
"""
from __future__ import annotations

from collections import defaultdict, deque

CHANGE_LABELS = {"drop": "删除字段", "rename": "改名", "type": "改类型"}


def compute_impact(report: dict, changes: list, max_paths_per_change: int = 500) -> dict:
    nodes = report.get("nodes", {})
    objects = report.get("objects", {})
    adj: dict[str, list] = defaultdict(list)
    for e in report.get("edges", []):
        adj[e["src"]].append(e)

    # 未解析/部分解析语句引用的表 → 影响范围可能不完整
    incomplete_by_table: dict[str, list] = defaultdict(list)
    for st in report.get("statements", []):
        if st.get("status") in ("unresolved", "partial"):
            for ref in st.get("refs", []):
                incomplete_by_table[ref].append(st)
                incomplete_by_table[ref.rsplit(".", 1)[-1]].append(st)

    results = []
    for change in changes:
        table = str(change.get("table", "")).lower()
        column = str(change.get("column", "")).lower()
        kind = change.get("change", "")
        entry = {
            "table": table,
            "column": column,
            "change": kind,
            "change_label": CHANGE_LABELS.get(kind, kind),
            "new_name": change.get("new_name"),
            "new_type": change.get("new_type"),
            "found": False,
            "affected": [],
            "caveats": [],
        }

        # 定位起点节点：允许未限定名唯一后缀匹配
        start = _find_column_node(nodes, objects, table, column)
        if start is None:
            entry["caveats"].append(
                f"字段 '{table}.{column}' 不在已知血缘中；"
                "若它只被未解析的语句使用，此处无法体现")
        else:
            entry["found"] = True
            entry["start_node"] = start
            entry["affected"] = _bfs_impact(start, adj, nodes, max_paths_per_change)

        # 未解析/部分解析语句的提示
        seen_stmt = set()
        for key in {table, table.rsplit(".", 1)[-1]}:
            for st in incomplete_by_table.get(key, []):
                if st["id"] in seen_stmt:
                    continue
                seen_stmt.add(st["id"])
                entry["caveats"].append(
                    f"语句 {st['id']}（{st['file']}:{st['line']}，状态 {st['status']}）"
                    f"引用了表 '{table}' 但未被完整解析，其影响需人工核对")
        results.append(entry)

    return {
        "meta": {
            "generated_from": report.get("meta", {}),
            "complete": report.get("stats", {}).get("complete", False),
        },
        "changes": results,
    }


def _find_column_node(nodes: dict, objects: dict, table: str, column: str) -> str | None:
    direct = f"{table}.{column}"
    if direct in nodes:
        return direct
    matches = [name for name in objects
               if name == table or name.rsplit(".", 1)[-1] == table]
    if len(matches) == 1:
        candidate = f"{matches[0]}.{column}"
        if candidate in nodes:
            return candidate
    return None


def _bfs_impact(start: str, adj: dict, nodes: dict, limit: int) -> list:
    """从起点字段做 BFS，记录每个受影响的持久字段及最短路径。

    每个受影响位置记录两条边的信息：
      - 首条边（direct_*）：被改字段被直接消费的表达式，核对类型/改名时最关键；
      - 末条边（expression/location 等）：影响传导到该位置的最后一步。
    """
    affected: dict[str, dict] = {}
    visited = {start}
    queue = deque([(start, [start], None)])
    truncated = False
    while queue:
        node, path, first_edge = queue.popleft()
        for edge in adj.get(node, []):
            dst = edge["dst"]
            if dst in path:
                continue  # 路径内防循环
            new_path = path + [dst]
            hop1 = first_edge or edge
            nd = nodes.get(dst)
            if nd and nd["kind"] == "column" and dst not in affected:
                affected[dst] = {
                    "object": nd["object"],
                    "column": nd["column"],
                    "node": dst,
                    "via": new_path,
                    "edge_kind": edge["kind"],
                    "transform": edge["transform"],
                    "expression": edge["expression"],
                    "location": edge["location"],
                    "direct_expression": hop1["expression"],
                    "direct_location": hop1["location"],
                }
                if len(affected) >= limit:
                    truncated = True
            if dst not in visited:
                visited.add(dst)
                queue.append((dst, new_path, hop1))
        if truncated:
            break
    result = sorted(affected.values(), key=lambda a: (a["object"], a["column"]))
    if truncated:
        result.append({"truncated": True,
                       "note": f"受影响位置超过 {limit} 个，仅列出前 {limit} 个"})
    return result
