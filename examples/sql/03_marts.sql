-- 03_marts.sql：集市层
-- 覆盖：CTAS、内联派生表、INSERT ... BY NAME（DuckDB 语法）、跨视图引用

CREATE TABLE mart_customer_ltv AS
SELECT
    v.customer_id,
    v.full_name,
    v.region,
    v.total_amount,
    v.amount_rank,
    d.revenue AS region_revenue
FROM v_customer_360 v
LEFT JOIN (
    SELECT region, sum(revenue) AS revenue
    FROM v_region_daily
    GROUP BY region
) d ON d.region = v.region;

INSERT INTO daily_metrics BY NAME
SELECT
    metric_date,
    region,
    revenue,
    order_cnt,
    NULL AS customer_cnt
FROM v_region_daily;
