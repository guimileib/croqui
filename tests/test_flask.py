"""FlaskScanner against the sample project in tests/fixtures/flask_app."""

from __future__ import annotations

import unittest
from pathlib import Path

from croqui.adapters.django_py import DjangoScanner
from croqui.adapters.flask_py import FlaskScanner
from croqui.model import Graph
from croqui.tables import build_registry

FIXTURES = Path(__file__).parent / "fixtures"
APP = FIXTURES / "flask_app"


def scan(root: Path):
    registry = build_registry(root)
    FlaskScanner.register_tables(root, registry)
    graph = Graph({})
    scanner = FlaskScanner(root, graph, registry)
    scanner.scan()
    return graph, registry


class FlaskDetectTests(unittest.TestCase):
    def test_detects_flask_project(self) -> None:
        self.assertTrue(FlaskScanner.detect(APP))

    def test_fastapi_only_is_not_flask(self) -> None:
        self.assertFalse(FlaskScanner.detect(FIXTURES / "fastapi_only"))

    def test_other_stacks_are_not_flask(self) -> None:
        self.assertFalse(FlaskScanner.detect(FIXTURES / "django_app"))
        self.assertFalse(DjangoScanner.detect(APP))


class FlaskTableTests(unittest.TestCase):
    def test_default_snake_case_table_names(self) -> None:
        _, registry = scan(APP)
        names = {r["name"] for r in registry.all()}
        # explicit __tablename__ plus Flask-SQLAlchemy defaults (no __tablename__ declared)
        self.assertTrue({"orders", "user", "user_profile", "order_item"} <= names)

    def test_columns_include_mixin_and_fk(self) -> None:
        _, registry = scan(APP)
        columns = {c["name"]: c for c in registry.resolve("user_profile")["columns"]}
        self.assertEqual(columns["user_id"]["references"], "user")
        user_cols = {c["name"] for c in registry.resolve("user")["columns"]}
        self.assertIn("created_at", user_cols)  # inherited from TimestampMixin


class FlaskRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph, cls.registry = scan(APP)
        cls.edges = {(e["from"], e["to"], e["kind"]): e for e in cls.graph.edges}

    def edge(self, src: str, dst: str, kind: str) -> dict:
        self.assertIn((src, dst, kind), self.edges, f"missing edge {src} -> {dst} ({kind})")
        return self.edges[(src, dst, kind)]

    def test_blueprint_prefix_and_converters(self) -> None:
        for node_id in (
            "route:GET:/orders/",
            "route:POST:/orders/",
            "route:DELETE:/orders/{order_id}",
            "route:GET:/orders/{order_id}/items",
        ):
            self.assertIn(node_id, self.graph.nodes)
        node = self.graph.nodes["route:GET:/orders/"]
        self.assertEqual(node["kind"], "route")
        self.assertEqual(node["label"], "GET /orders/")
        self.assertEqual(node["meta"]["handler"], "list_orders")
        self.assertEqual(node["meta"]["description"], "List every order.")
        self.assertTrue(node["meta"]["source"].startswith("app/blueprints/orders.py:"))

    def test_nested_and_registration_prefixes(self) -> None:
        # users_bp (/users) nested in api_bp (/api); reports_bp mounted with url_prefix
        self.assertIn("route:GET:/api/users/", self.graph.nodes)
        self.assertIn("route:GET:/reports/revenue", self.graph.nodes)
        self.assertNotIn("route:GET:/users/", self.graph.nodes)

    def test_app_level_get_decorator_is_standalone(self) -> None:
        node = self.graph.nodes["route:GET:/health"]
        self.assertTrue(node["meta"].get("standalone"))

    def test_methodview_one_route_per_verb(self) -> None:
        self.assertIn("route:GET:/api/users/{user_id}", self.graph.nodes)
        self.assertIn("route:DELETE:/api/users/{user_id}", self.graph.nodes)
        self.assertNotIn("route:POST:/api/users/{user_id}", self.graph.nodes)
        self.assertIn("route:POST:/api/users/", self.graph.nodes)
        self.assertEqual(self.graph.nodes["route:POST:/api/users/"]["meta"]["handler"], "UserCollection.post")

    def test_route_service_repo_table_chain(self) -> None:
        self.edge("route:POST:/orders/", "service:OrderService", "calls")
        self.edge("service:OrderService", "repo:OrderRepository", "calls")
        edge = self.edge("repo:OrderRepository", "table:orders", "write")
        self.assertEqual(set(edge["ops"]), {"SELECT", "INSERT", "DELETE"})
        self.assertNotIn("standalone", self.graph.nodes["route:POST:/orders/"]["meta"])

    def test_service_external_call(self) -> None:
        self.edge("service:OrderService", "external:HTTP / Webhook", "send")

    def test_direct_orm_route_to_table(self) -> None:
        edge = self.edge("route:GET:/orders/{order_id}/items", "table:order_item", "read")
        self.assertEqual(edge["ops"], ["SELECT"])

    def test_methodview_ops_per_verb(self) -> None:
        self.assertEqual(self.edge("route:GET:/api/users/", "table:user", "read")["ops"], ["SELECT"])
        self.assertEqual(self.edge("route:POST:/api/users/", "table:user", "write")["ops"], ["INSERT"])
        ops = self.edge("route:DELETE:/api/users/{user_id}", "table:user", "write")["ops"]
        self.assertEqual(set(ops), {"SELECT", "DELETE"})

    def test_inline_sql_and_helpers(self) -> None:
        # handler -> _revenue_rows() helper -> text("... FROM orders JOIN user ...")
        self.edge("route:GET:/reports/revenue", "table:orders", "read")
        self.edge("route:GET:/reports/revenue", "table:user", "read")
        # module-level SQL constant
        self.edge("route:GET:/reports/top-skus", "table:order_item", "read")

    def test_dynamic_sql_is_flagged_not_guessed(self) -> None:
        node = self.graph.nodes["route:GET:/reports/adhoc/{name}"]
        self.assertTrue(node["meta"].get("unresolved"))
        self.assertFalse([e for e in self.graph.edges if e["from"] == node["id"]])

    def test_only_registry_tables_become_nodes(self) -> None:
        tables = {n["label"] for n in self.graph.nodes.values() if n["kind"] == "table"}
        self.assertTrue(tables <= {r["name"] for r in self.registry.all()})


if __name__ == "__main__":
    unittest.main()
