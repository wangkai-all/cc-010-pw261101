"""表结构注册表：合并 schema 清单（manifest）与 SQL 里的 DDL。"""
from __future__ import annotations

from .model import ObjectDef


def norm_name(name: str) -> str:
    """对象名规范化：逐段小写（DuckDB 未加引号的标识符不区分大小写）。"""
    return ".".join(part.strip().lower() for part in name.split(".") if part.strip())


class SchemaRegistry:
    def __init__(self):
        self.objects: dict[str, ObjectDef] = {}

    def load_manifest(self, data: dict, defined_by: dict | None = None) -> list:
        """加载表结构清单 JSON，返回告警列表。"""
        warnings = []
        defined_by = defined_by or {}
        for name, spec in (data.get("tables") or {}).items():
            key = norm_name(name)
            cols = [
                {"name": str(c["name"]).lower(), "type": str(c.get("type", ""))}
                for c in spec.get("columns", [])
            ]
            if key in self.objects:
                warnings.append(f"对象 '{key}' 在清单中重复定义，以后者为准")
            self.objects[key] = ObjectDef(
                name=key,
                kind=spec.get("kind", "table"),
                columns=cols,
                source="manifest",
                defined_by=defined_by,
            )
        return warnings

    def register(self, obj: ObjectDef) -> str | None:
        """注册对象，返回告警（重复定义时）。"""
        warning = None
        if obj.name in self.objects:
            warning = f"对象 '{obj.name}' 被重复定义，以后者为准"
        self.objects[obj.name] = obj
        return warning

    def unregister(self, name: str) -> None:
        self.objects.pop(norm_name(name), None)

    def find(self, name: str) -> str | None:
        """按引用名查找对象 key；未限定名允许唯一后缀匹配（如 orders 匹配 main.orders）。"""
        key = norm_name(name)
        if key in self.objects:
            return key
        if "." not in key:
            matches = [k for k in self.objects if k.rsplit(".", 1)[-1] == key]
            if len(matches) == 1:
                return matches[0]
        return None

    def get(self, name: str) -> ObjectDef | None:
        key = self.find(name)
        return self.objects.get(key) if key else None
