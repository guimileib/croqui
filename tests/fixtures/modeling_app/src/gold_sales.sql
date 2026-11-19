-- Databricks notebook source
CREATE OR REFRESH MATERIALIZED VIEW gold_revenue_by_customer
AS SELECT c.id, sum(o.total) AS revenue
FROM LIVE.silver_orders o
JOIN LIVE.silver_customers c ON c.id = o.customer_id
GROUP BY c.id;
