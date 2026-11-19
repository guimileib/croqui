"""The modelagem tab: DER, data pipelines and agent systems, read statically from source."""

from __future__ import annotations

import unittest
from pathlib import Path

from croqui.modeling import (
    build_modeling,
    load_yaml,
    medallion_layer,
    modeling_docs,
    modeling_text,
)
from croqui.tables import build_registry

FIXTURE = Path(__file__).parent / "fixtures" / "modeling_app"


def _model() -> dict:
    return build_modeling(FIXTURE, build_registry(FIXTURE, code_suffixes=(".py",)))


def _edges(item: dict) -> set[tuple[str, str]]:
    return {(e["from"], e["to"]) for e in item["edges"]}


class DerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = _model()

    def test_entities_carry_their_columns(self) -> None:
        names = {e["name"] for e in self.model["entities"]}
        self.assertEqual(names, {"customers", "orders", "order_items", "products"})
        orders = next(e for e in self.model["entities"] if e["name"] == "orders")
        pk = [c["name"] for c in orders["columns"] if c.get("pk")]
        self.assertEqual(pk, ["id"])

    def test_table_level_foreign_key_is_read(self) -> None:
        # `CONSTRAINT … FOREIGN KEY (customer_id) REFERENCES customers(id)`
        rel = next(r for r in self.model["relations"] if r["from"] == "orders")
        self.assertEqual((rel["to"], rel["column"], rel["confidence"]), ("customers", "customer_id", "static"))
        self.assertTrue(rel["required"])

    def test_inline_reference_and_inferred_fk(self) -> None:
        by_col = {r["column"]: r for r in self.model["relations"] if r["from"] == "order_items"}
        self.assertEqual(by_col["order_id"]["confidence"], "static")
        # product_id has no REFERENCES, but a `products` table exists.
        self.assertEqual(by_col["product_id"]["to"], "products")
        self.assertEqual(by_col["product_id"]["confidence"], "inferred")

    def test_er_mermaid(self) -> None:
        der = next(d for d in modeling_docs(self.model) if d["group"] == "der")
        self.assertIn("erDiagram", der["text"])
        self.assertIn('orders }|--|| customers : "customer_id"', der["text"])
        self.assertIn("order_items }o..|| products", der["text"])  # inferred: dashed


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pipes = {p["kind"]: p for p in _model()["pipelines"]}

    def test_databricks_bundle_job(self) -> None:
        job = self.pipes["databricks-job"]
        self.assertEqual(job["name"], "Daily sales refresh")
        self.assertEqual(job["schedule"], "0 0 6 * * ?")
        types = {s["id"]: s["type"] for s in job["steps"]}
        self.assertEqual(types, {"ingest": "notebook", "refresh_dlt": "pipeline",
                                 "check_quality": "condition", "publish": "sql"})
        self.assertEqual(_edges(job), {("ingest", "refresh_dlt"), ("refresh_dlt", "check_quality"),
                                       ("check_quality", "publish")})
        outcome = next(e for e in job["edges"] if e["to"] == "publish")
        self.assertEqual(outcome.get("label"), "true")
        # The pipeline task names the DLT pipeline it triggers.
        refresh = next(s for s in job["steps"] if s["id"] == "refresh_dlt")
        self.assertEqual(refresh["detail"], "DLT Sales DLT")

    def test_dlt_lineage_across_python_and_sql(self) -> None:
        dlt = self.pipes["dlt"]
        self.assertEqual(dlt["name"], "Sales DLT")
        self.assertEqual(dlt["target"], "main.sales")
        edges = _edges(dlt)
        self.assertIn(("@/mnt/raw/orders", "bronze_orders"), edges)
        self.assertIn(("bronze_orders", "silver_orders"), edges)
        self.assertIn(("silver_orders", "gold_revenue_by_customer"), edges)  # from the .sql file
        self.assertIn(("silver_customers", "gold_revenue_by_customer"), edges)
        layers = {s["id"]: s.get("layer") for s in dlt["steps"]}
        self.assertEqual(layers["bronze_orders"], "bronze")
        self.assertEqual(layers["gold_revenue_by_customer"], "gold")
        silver = next(s for s in dlt["steps"] if s["id"] == "silver_orders")
        self.assertEqual(silver["expectations"], 1)
        self.assertTrue(silver["streaming"])
        # Evidence is project-relative, like everywhere else in croqui.
        for step in dlt["steps"]:
            if step.get("evidence"):
                self.assertTrue(step["evidence"].startswith("src/"), step["evidence"])

    def test_airflow_dag(self) -> None:
        dag = self.pipes["airflow"]
        self.assertEqual(dag["name"], "nightly_etl")
        self.assertEqual(_edges(dag), {("extract", "transform"), ("transform", "decide"),
                                       ("decide", "load"), ("decide", "notify")})
        types = {s["id"]: s["type"] for s in dag["steps"]}
        self.assertEqual(types["decide"], "branch")

    def test_dbt_refs_and_sources(self) -> None:
        dbt = self.pipes["dbt"]
        self.assertEqual(_edges(dbt), {("raw.orders", "stg_orders"), ("stg_orders", "fct_revenue")})
        layers = {s["id"]: s.get("layer") for s in dbt["steps"]}
        self.assertEqual((layers["stg_orders"], layers["fct_revenue"]), ("silver", "gold"))

    def test_medallion_words(self) -> None:
        self.assertEqual(medallion_layer("raw_events"), "bronze")
        self.assertEqual(medallion_layer("stg_orders"), "silver")
        self.assertEqual(medallion_layer(None, "marts"), "gold")
        self.assertIsNone(medallion_layer("orders"))


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.systems = {s["kind"]: s for s in _model()["agents"]}

    def test_langgraph_nodes_router_and_tools(self) -> None:
        graph = self.systems["langgraph"]
        self.assertEqual(graph["name"], "support_graph (builder)")
        edges = _edges(graph)
        self.assertIn(("__start__", "agent"), edges)
        # The router has no path map; its `return` statements are read instead.
        self.assertIn(("agent", "tools"), edges)
        self.assertIn(("agent", "__end__"), edges)
        self.assertIn(("tools", "agent"), edges)
        self.assertIn(("tools", "tool:search_orders"), edges)
        tools = next(n for n in graph["nodes"] if n["id"] == "tools")
        self.assertEqual(tools["type"], "tools")

    def test_crewai_tasks_run_in_order(self) -> None:
        crew = self.systems["crewai"]
        edges = _edges(crew)
        self.assertIn(("__start__", "task:research"), edges)
        self.assertIn(("task:research", "task:report"), edges)
        self.assertIn(("task:report", "agent:writer"), edges)
        roles = {n["label"] for n in crew["nodes"] if n["type"] == "agent"}
        self.assertEqual(roles, {"Analista de mercado", "Redator"})

    def test_agents_sdk_handoffs_group_into_one_system(self) -> None:
        triage = self.systems["agents"]
        self.assertEqual(triage["name"], "Triage agent")
        edges = _edges(triage)
        self.assertIn(("agent:triage", "agent:billing"), edges)
        self.assertIn(("agent:triage", "agent:support"), edges)
        self.assertIn(("agent:billing", "tool:refund"), edges)
        self.assertIn(("__start__", "agent:triage"), edges)

    def test_claude_code_subagents(self) -> None:
        sub = self.systems["claude-code"]
        self.assertIn(("agent:reviewer", "tool:Grep"), _edges(sub))

    def test_agent_without_llm_import_is_ignored(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crm.py").write_text(
                "class Agent:\n    pass\n\nalice = Agent(name='Alice')\n", encoding="utf-8"
            )
            self.assertEqual(build_modeling(root, build_registry(root))["agents"], [])


class ViewTests(unittest.TestCase):
    def test_one_document_per_view(self) -> None:
        docs = modeling_docs(_model())
        groups = [d["group"] for d in docs]
        self.assertEqual(groups.count("der"), 1)
        self.assertEqual(groups.count("pipeline"), 4)
        self.assertEqual(groups.count("agents"), 4)
        for doc in docs:
            self.assertTrue(doc["name"].startswith("modelagem/"))
            self.assertTrue(doc["text"].startswith("---\ntitle: "))

    def test_pipeline_flowchart_has_medallion_frames(self) -> None:
        docs = {d["title"]: d["text"] for d in modeling_docs(_model())}
        dlt = docs["Sales DLT"]
        self.assertIn('subgraph L_bronze["bronze · bruto"]', dlt)
        self.assertIn(":::gold", dlt)
        self.assertIn("-.->|stream|", dlt)

    def test_context_text(self) -> None:
        text = "\n".join(modeling_text(_model()))
        self.assertIn("`orders.customer_id` → `customers` (N:1)", text)
        self.assertIn("Triage agent passa para Billing agent [handoff]", text)

    def test_empty(self) -> None:
        self.assertEqual(modeling_docs(None), [])
        self.assertEqual(modeling_text({}), [])


class YamlTests(unittest.TestCase):
    def test_subset(self) -> None:
        data = load_yaml(
            "a:\n"
            "  b: 1  # comment\n"
            "  list:\n"
            "    - k: x\n"
            "      deps:\n"
            "        - k: y\n"
            "    - plain\n"
            "  flow: [one, 'two']\n"
            "  map: {x: 1, y: z}\n"
            "  text: |\n"
            "    line one\n"
            "    line: two\n"
            "after: 'q # not a comment'\n"
        )
        self.assertEqual(data["a"]["b"], "1")
        self.assertEqual(data["a"]["list"][0], {"k": "x", "deps": [{"k": "y"}]})
        self.assertEqual(data["a"]["list"][1], "plain")
        self.assertEqual(data["a"]["flow"], ["one", "two"])
        self.assertEqual(data["a"]["map"], {"x": "1", "y": "z"})
        self.assertEqual(data["a"]["text"], "line one\nline: two")
        self.assertEqual(data["after"], "q # not a comment")

    def test_garbage_does_not_raise(self) -> None:
        self.assertIsNone(load_yaml(""))
        load_yaml("- - -\n  : :\n{[")


if __name__ == "__main__":
    unittest.main()
