-- 01_ddl.sql：显式建表 + INSERT SELECT
-- 覆盖：字段列表映射、计算列、字符串字面量里出现表名（不应产生血缘）

CREATE TABLE stg_orders (
    order_id     BIGINT,
    customer_id  BIGINT,
    amount       DOUBLE,
    quantity     INTEGER,
    status       VARCHAR,
    ordered_at   TIMESTAMP,
    note         VARCHAR
);

INSERT INTO stg_orders (order_id, customer_id, amount, quantity, status, ordered_at, note)
SELECT
    o.order_id,
    o.customer_id,
    CAST(o.amount AS DECIMAL(18, 2)) * o.quantity AS amount,
    o.quantity,
    lower(o.status) AS status,
    o.ordered_at,
    'loaded from raw_orders o join raw_customers c on c.customer_id = o.customer_id' AS note
FROM raw_orders o;

CREATE TABLE daily_metrics (
    metric_date  DATE,
    region       VARCHAR,
    revenue      DOUBLE,
    order_cnt    BIGINT,
    customer_cnt BIGINT
);
