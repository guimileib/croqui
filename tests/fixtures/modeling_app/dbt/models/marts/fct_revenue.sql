{{ config(materialized='table') }}
select customer_id, sum(total) as revenue from {{ ref('stg_orders') }} group by 1
