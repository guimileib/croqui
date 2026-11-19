# Databricks notebook source
import dlt
from pyspark.sql import functions as F


@dlt.table(name="bronze_orders", comment="raw orders")
def bronze_orders():
    return spark.readStream.format("cloudFiles").option("cloudFiles.format", "json").load("/mnt/raw/orders")


@dlt.table
@dlt.expect_or_drop("valid_total", "total > 0")
def silver_orders():
    return dlt.read_stream("bronze_orders").where(F.col("total").isNotNull())


@dlt.table
def silver_customers():
    return spark.read.table("main.crm.customers")
