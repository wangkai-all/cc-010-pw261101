-- 04_broken.sql：各种无法解析 / 暂不支持的情况
-- 这些语句必须被明确标记为"未解析"，并提示血缘不完整

-- 1. 语法错误
SELEC order_id, amount FRM stg_orders WHERE;

-- 2. 递归 CTE（暂不支持）
WITH RECURSIVE seq(n) AS (
    SELECT 1
    UNION ALL
    SELECT n + 1 FROM seq WHERE n < 10
)
SELECT count(*) AS cnt FROM seq;

-- 3. PIVOT（暂不支持）
SELECT *
FROM stg_orders
PIVOT (sum(amount) FOR status IN ('paid', 'shipped'));

-- 4. 引用不存在的表（且引用了 raw_orders，影响分析时应列为"需人工核对"）
CREATE VIEW v_ghost AS
SELECT g.unknown_col, o.amount
FROM ghost_table g
JOIN raw_orders o ON o.order_id = g.id;
