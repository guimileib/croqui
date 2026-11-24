"""The modelagem tab: DER, data pipelines and agent systems, read statically from source."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from croqui.model import Graph, clean_layout
from croqui.modeling import (
    add_agents,
    build_modeling,
    load_yaml,
    medallion_layer,
    modeling_docs,
    modeling_text,
)
from croqui.tables import TableRegistry, build_registry

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


SCHEMAS_DDL = """
CREATE TABLE access_v3.module (module_code text PRIMARY KEY, name text);
CREATE TABLE internal_v3.module (module_code text PRIMARY KEY, action text);
CREATE TABLE access_v3.profile_module (
    profile_code text,
    module_code text REFERENCES module(module_code)
);
CREATE TABLE internal_v3.staff (staff_id uuid PRIMARY KEY);
CREATE TABLE legacy.staff (id integer PRIMARY KEY);
CREATE TABLE internal_v3.staff_module (
    staff_id uuid,
    module_code text,
    CONSTRAINT fk_staff_module FOREIGN KEY (module_code) REFERENCES internal_v3.module(module_code)
);
CREATE TABLE users_v3.person (user_id uuid PRIMARY KEY);
CREATE TABLE access_v3.store_access (id uuid PRIMARY KEY, user_id uuid REFERENCES users_v3.person(user_id));
CREATE TABLE security.tb_user (id integer PRIMARY KEY);
"""

SCHEMAS_ORM = '''
class Session(Base):
    __tablename__ = "tb_session"
    __table_args__ = {"schema": "security"}
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("security.tb_user.id"))
'''


class SchemaQualifiedTests(unittest.TestCase):
    """Same table name in two schemas: two tables, each with its own columns and lines."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schema.sql").write_text(SCHEMAS_DDL, encoding="utf-8")
            (root / "models.py").write_text(SCHEMAS_ORM, encoding="utf-8")
            cls.registry = build_registry(root, code_suffixes=(".py",))
            cls.model = build_modeling(root, cls.registry)

    def _rel(self, source: str, column: str) -> dict:
        return next(r for r in self.model["relations"] if r["from"] == source and r["column"] == column)

    def test_same_name_in_two_schemas_stays_two_tables(self) -> None:
        cols = {e["id"]: [c["name"] for c in e["columns"]] for e in self.model["entities"]}
        self.assertEqual(cols["access_v3.module"], ["module_code", "name"])
        self.assertEqual(cols["internal_v3.module"], ["module_code", "action"])

    def test_qualified_reference_hits_its_schema(self) -> None:
        self.assertEqual(self._rel("internal_v3.staff_module", "module_code")["to"], "internal_v3.module")
        self.assertEqual(self._rel("access_v3.store_access", "user_id")["to"], "users_v3.person")

    def test_bare_reference_prefers_its_own_schema(self) -> None:
        self.assertEqual(self._rel("access_v3.profile_module", "module_code")["to"], "access_v3.module")
        inferred = self._rel("internal_v3.staff_module", "staff_id")
        self.assertEqual((inferred["to"], inferred["confidence"]), ("internal_v3.staff", "inferred"))

    def test_orm_foreign_key_keeps_the_schema(self) -> None:
        self.assertEqual(self._rel("security.tb_session", "user_id")["to"], "security.tb_user")

    def test_resolve(self) -> None:
        self.assertEqual(self.registry.resolve("internal_v3.module")["schema"], "internal_v3")
        self.assertEqual(self.registry.qualified('"access_v3"."module"'), "access_v3.module")
        self.assertIsNone(self.registry.resolve("module"))  # ambiguous without a schema
        self.assertEqual(self.registry.qualified("person"), "users_v3.person")  # the only one

    def test_schema_backfills_a_bare_record(self) -> None:
        registry = TableRegistry()
        registry.add("orders", schema=None, kind="table", source="models.py:3")
        registry.add("orders", schema="sales", kind="table", source="schema.sql")
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.qualified("orders"), "sales.orders")


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


class AgentMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = Graph({})
        add_agents(cls.graph, _model()["agents"])
        cls.by_label = {n["label"]: n for n in cls.graph.nodes.values()}

    def test_agents_and_tools_become_map_nodes(self) -> None:
        triage = self.by_label["Triage agent"]
        self.assertEqual((triage["kind"], triage["group"]), ("agent", "Triage agent"))
        self.assertEqual(self.by_label["refund"]["kind"], "tool")
        self.assertIn("triage.py", triage["meta"]["source"])

    def test_handoffs_become_edges_and_start_end_are_left_out(self) -> None:
        pairs = {(e["from"], e["to"]) for e in self.graph.edges}
        self.assertIn((self.by_label["Triage agent"]["id"], self.by_label["Billing agent"]["id"]), pairs)
        self.assertIn((self.by_label["Billing agent"]["id"], self.by_label["refund"]["id"]), pairs)
        self.assertEqual(self.graph.drop_dangling_edges(), 0)
        self.assertFalse([i for i in self.graph.nodes if "__start__" in i or "__end__" in i])


class MigrationTests(unittest.TestCase):
    """Foreign keys declared after the table: ALTER TABLE in a docker init script, Alembic revisions."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parent / "fixtures" / "migrations_app"
        model = build_modeling(root, build_registry(root))
        cls.entities = {e["id"]: e for e in model["entities"]}
        cls.relations = {(r["from"], r["column"]): r["to"] for r in model["relations"] if r["confidence"] == "static"}

    def test_alter_table_adds_keys_and_columns(self) -> None:
        self.assertEqual(self.relations[("public.invoices", "account_id")], "public.accounts")
        self.assertEqual(self.relations[("public.payments", "invoice_ref")], "public.invoices")
        accounts = {c["name"]: c for c in self.entities["public.accounts"]["columns"]}
        self.assertTrue(accounts["id"].get("pk"))

    def test_alembic_revisions(self) -> None:
        self.assertEqual(self.relations[("members", "team_id")], "teams")
        self.assertEqual(self.relations[("members", "owner")], "members")
        # op.add_column + op.create_foreign_key, in a later revision
        self.assertEqual(self.relations[("badges", "holder")], "members")
        teams = {c["name"]: c for c in self.entities["teams"]["columns"]}
        self.assertTrue(teams["id"].get("pk"))


class LayoutTests(unittest.TestCase):
    def test_diagram_positions_survive_a_save(self) -> None:
        clean = clean_layout({"diagrams": {
            "modelagem/der.mmd": {"orders": {"x": 10, "y": 20.5, "junk": 1}, "bad": {"x": "1"}},
            "empty": {}, "wrong": [1],
        }})
        self.assertEqual(clean["diagrams"], {"modelagem/der.mmd": {"orders": {"x": 10, "y": 20.5}}})
        self.assertNotIn("diagrams", clean_layout({}))

    def test_relation_bends_survive_a_save(self) -> None:
        clean = clean_layout({"bends": {
            "prd/a.md#1": {"orders>users>user_id": {"x": -40, "y": 12, "junk": 1}, "a>b>": 7, "c>d>": {"x": "1"}},
            "empty": {},
        }})
        self.assertEqual(clean["bends"], {"prd/a.md#1": {"orders>users>user_id": {"x": -40, "y": 12}}})
        self.assertNotIn("bends", clean_layout({}))


class ViewTests(unittest.TestCase):
    def test_one_document_per_view(self) -> None:
        docs = modeling_docs(_model())
        groups = [d["group"] for d in docs]
        self.assertEqual(groups.count("der"), 1)
        self.assertEqual(groups.count("pipeline"), 4)
        # Agent systems are nodes of the map, not documents of this tab.
        self.assertEqual(groups.count("agents"), 0)
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
