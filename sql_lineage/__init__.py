"""sql_lineage：DuckDB 方言的字段级 SQL 血缘分析工具。"""

__version__ = "0.1.0"

from .analyzer import Analyzer  # noqa: F401
from .impact import compute_impact  # noqa: F401
