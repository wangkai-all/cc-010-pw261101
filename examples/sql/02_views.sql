-- 02_views.sql：视图层
-- 覆盖：CTE、星号展开（EXCLUDE）、CTE 名称遮蔽基础表、多层派生、
--       窗口函数、聚合、UNION ALL 两分支复用同一表别名

-- 视图 1：CTE + 星号展开 + 字符串里的表名（join_hint 不应产生血缘）
CREATE OR REPLACE VIEW v_order_clean AS
WITH src AS (
    SELECT * EXCLUDE (note)
    FROM stg_orders
    WHERE status <> 'cancelled'
)
SELECT
    src.*,
    amount * 0.13 AS tax,
    'join raw_customers c on c.customer_id = src.customer_id' AS join_hint
FROM src;

-- 视图 2：多层 CTE；CTE 名叫 raw_orders，遮蔽同名基础表
CREATE OR REPLACE VIEW v_customer_360 AS
WITH raw_orders AS (
    SELECT order_id, customer_id, amount, ordered_at
    FROM raw_orders
    WHERE status IN ('paid', 'shipped')
),
per_customer AS (
    SELECT
        customer_id,
        count(*)        AS order_cnt,
        sum(amount)     AS total_amount
    FROM raw_orders          -- 这里引用的是上面的 CTE，不是基础表
    GROUP BY customer_id
),
ranked AS (
    SELECT
        customer_id,
        total_amount,
        row_number() OVER (ORDER BY total_amount DESC) AS amount_rank
    FROM per_customer
)
SELECT
    c.customer_id,
    c.full_name,
    c.region,
    coalesce(r.total_amount, 0) AS total_amount,
    r.amount_rank,
    pc.order_cnt
FROM raw_customers c
LEFT JOIN ranked       r  ON r.customer_id  = c.customer_id
LEFT JOIN per_customer pc ON pc.customer_id = c.customer_id;

-- 视图 3：UNION ALL，两个分支都用了别名 o / c（别名复用，互不干扰）
CREATE OR REPLACE VIEW v_region_daily AS
SELECT
    CAST(o.ordered_at AS DATE) AS metric_date,
    c.region                   AS region,
    sum(o.amount)              AS revenue,
    count(*)                   AS order_cnt
FROM stg_orders o
JOIN raw_customers c ON c.customer_id = o.customer_id
WHERE o.status = 'paid'
GROUP BY 1, 2
UNION ALL
SELECT
    CAST(o.ordered_at AS DATE) AS metric_date,
    c.region                   AS region,
    sum(o.amount)              AS revenue,
    count(*)                   AS order_cnt
FROM raw_orders o
JOIN raw_customers c ON c.customer_id = o.customer_id
WHERE o.status = 'shipped'
GROUP BY 1, 2;
