# sql_lineage — DuckDB 字段级 SQL 血缘分析工具

读取**表结构清单**和一组 SQL（建表 / 建视图 / 写入查询），用 sqlglot 按 DuckDB 方言解析，
输出**字段级**血缘：每个输出字段依赖哪些源字段；点击任一源字段，能看到它影响的
下游表、字段以及经过 CTE / 子查询的完整中间路径——不只是表与表之间的连线。

## 功能

- **字段级血缘图**：每个查询层（语句顶层、CTE、派生表、标量子查询）都建节点，
  边只连相邻两层，下游路径可逐级还原中间链路，边上保留表达式与来源位置（文件:行号）。
- **方言覆盖**：多层 CTE、子查询、表别名、字段别名、JOIN、UNION ALL（含 BY NAME）、
  聚合、窗口函数、`INSERT INTO ... (列列表) / BY NAME / 位置对齐`、CTAS。
- **`SELECT *` 按对应层的表结构展开**，支持 DuckDB 的 `* EXCLUDE / REPLACE / RENAME`。
- **易误判场景**：同名字段（标记歧义并保守连接所有候选）、CTE 名称遮蔽基础表、
  字符串字面量里的表名（基于 AST 解析，天然不误判）、lambda 参数、
  WHERE/HAVING/ORDER BY 引用 SELECT 别名（DuckDB 允许）。
- **引用边（usage）**：WHERE / GROUP BY / HAVING / QUALIFY / JOIN ON 里出现的字段
  不产生输出值，但删除会破坏语句——影响分析同样覆盖。
- **变更影响分析**：输入变更清单，分别列出**删除字段 / 改名 / 改类型**影响的下游位置，
  附完整传导路径和被改字段的直接引用表达式，方便核对。
- **诚实的不完整提示**：无法解析或暂不支持的语句明确标为 `unresolved`，
  报告头部和网页横幅都会警告"血缘不完整"；未解析语句引用了被改表时，
  影响分析会列出"需人工核对"。
- **循环依赖**：对象间成环时指出循环路径（如 `cyc_a → cyc_b → cyc_a`），不无限递归。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # sqlglot + pytest
```

## 快速开始

```bash
# 1. 分析：表结构清单 + SQL 目录 → 血缘 JSON + 网页
.venv/bin/python -m sql_lineage analyze \
    --schema examples/schema.json \
    --sql examples/sql \
    --json out/lineage.json \
    --html out/lineage.html

# 2. 影响分析：血缘 JSON + 变更清单 → 影响 JSON + 含影响视图的网页
.venv/bin/python -m sql_lineage impact \
    --lineage out/lineage.json \
    --changes examples/changes.json \
    --json out/impact.json \
    --html out/impact.html
```

直接用浏览器打开 `out/impact.html`（自包含、无外部依赖）：

- **血缘浏览**：左侧搜索并点击任意字段，右侧展开它的下游影响 / 上游来源路径树；
  紫色节点是语句内部的中间层（CTE / 子查询）。
- **影响分析**：按变更逐条列出受影响的下游位置，可展开查看传导路径与表达式。
- **语句与问题**：每条语句的解析状态、未解析原因、循环依赖路径。

## 输入格式

### 表结构清单 `--schema`（可选）

```json
{
  "tables": {
    "raw_orders": {
      "columns": [
        {"name": "order_id", "type": "BIGINT"},
        {"name": "amount", "type": "DOUBLE"}
      ]
    }
  }
}
```

SQL 里的 `CREATE TABLE` 会合并进 schema（重复定义以后者为准并提示）。
未提供清单时，只有 SQL 里显式建表 / CTAS 产生的对象可用；引用不到的对象会导致
该语句标记为未解析。

### 变更清单 `--changes`

```json
{
  "changes": [
    {"table": "raw_orders", "column": "status", "change": "drop"},
    {"table": "raw_customers", "column": "full_name", "change": "rename", "new_name": "customer_name"},
    {"table": "raw_orders", "column": "amount", "change": "type", "new_type": "DECIMAL(18,2)"}
  ]
}
```

## 输出说明

`lineage.json` 关键字段：

| 字段 | 含义 |
| --- | --- |
| `objects` | 全部表/视图及其字段（含来源：清单还是 SQL） |
| `nodes` | 血缘节点；`s5:cte.xxx.col` 形式的是语句 `s5` 的中间层 |
| `edges` | 字段级边：`kind`（value 值 / usage 引用）、`transform`（direct/expression/aggregate/window/setop）、`expression`、`location` |
| `statements` | 每条语句的状态：`ok / partial / unresolved / ignored` 及原因、告警 |
| `cycles` | 循环依赖路径 |
| `stats.complete` | 是否存在未解析/部分解析语句或循环（为 false 时血缘不完整） |

## 工作原理

1. **语句切分**（`splitter.py`）：状态机识别字符串 / 注释 / 美元引号，按分号切语句并记录行号；
   单条解析失败不影响其他语句。
2. **作用域解析**（`resolver.py`）：在 sqlglot 的 AST 上自建作用域链——CTE 遮蔽外层对象、
   内层遮蔽外层、别名限定、星号按层展开；表达式里的标量子查询 / EXISTS 递归建层。
3. **多趟解析**（`analyzer.py`）：语句可按任意顺序引用视图/CTAS 产物；每趟解析能解析的语句，
   直到没有进展。剩余语句区分"引用未定义对象"与"循环依赖"（在剩余语句的定义-引用图上找环）。
4. **影响分析**（`impact.py`）：从被改字段沿 value+usage 边 BFS（带访问集，不会死循环），
   记录每个受影响持久字段的最短路径、首条边的直接引用表达式。

## 明确不支持（会标为 `unresolved` 并提示血缘不完整）

- `WITH RECURSIVE`、`PIVOT`/`UNPIVOT`、`UNNEST`、`LATERAL`、`SELECT * ILIKE`、表函数（`read_csv` 等）
- `MERGE` / `UPDATE` / `DELETE` / `ALTER` / `CREATE MACRO` 等语句类型
- 语法错误、引用未定义对象、循环依赖

`SET` / `USE` / `DESCRIBE` 等与血缘无关的语句标为 `ignored`，不影响完整性判定。

其他已知限制：结构体字段访问（`s.col`）按未限定引用处理并告警；`SELECT ... INTO`
不注册目标表；加引号的标识符区分大小写，与清单的小写键可能不匹配。

## 样例与测试

`examples/` 自带一组互相关联的样例（不依赖外部数据库）：

- `sql/01_ddl.sql` 显式建表 + INSERT（字符串字面量里藏表名）
- `sql/02_views.sql` CTE、星号 EXCLUDE、CTE 遮蔽基础表、窗口/聚合、UNION ALL 别名复用
- `sql/03_marts.sql` CTAS、内联派生表、INSERT BY NAME
- `sql/04_broken.sql` 语法错误、递归 CTE、PIVOT、引用不存在的表
- `sql/05_cycle.sql` 两个视图互相引用成环
- `changes.json` 删除 / 改名 / 改类型各一条

```bash
.venv/bin/python -m pytest tests/   # 47 个测试
```
