from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.providers.databricks.operators.databricks import DatabricksRunNowOperator

with DAG("nightly_etl", start_date=datetime(2024, 1, 1), schedule="@daily") as dag:
    extract = PythonOperator(task_id="extract", python_callable=lambda: None)
    transform = PythonOperator(task_id="transform", python_callable=lambda: None)
    decide = BranchPythonOperator(task_id="decide", python_callable=lambda: "load")
    load = DatabricksRunNowOperator(task_id="load", job_id=42)
    notify = PythonOperator(task_id="notify", python_callable=lambda: None)

    extract >> transform >> decide >> [load, notify]
