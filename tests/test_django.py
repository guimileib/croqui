"""DjangoScanner against the sample project in tests/fixtures/django_app."""

from __future__ import annotations

import unittest
from pathlib import Path

from croqui.adapters.django_py import DjangoScanner, _regex_route
from croqui.adapters.flask_py import FlaskScanner
from croqui.model import Graph
from croqui.tables import build_registry

FIXTURES = Path(__file__).parent / "fixtures"
APP = FIXTURES / "django_app"


def scan(root: Path):
    registry = build_registry(root)
    DjangoScanner.register_tables(root, registry)
    graph = Graph({})
    DjangoScanner(root, graph, registry).scan()
    return graph, registry


class DjangoDetectTests(unittest.TestCase):
    def test_detects_django_project(self) -> None:
        self.assertTrue(DjangoScanner.detect(APP))

    def test_other_stacks_are_not_django(self) -> None:
        self.assertFalse(DjangoScanner.detect(FIXTURES / "fastapi_only"))
        self.assertFalse(DjangoScanner.detect(FIXTURES / "flask_app"))
        self.assertFalse(FlaskScanner.detect(APP))


class DjangoModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _, cls.registry = scan(APP)

    def test_table_names(self) -> None:
        names = {r["name"] for r in self.registry.all()}
        self.assertEqual(names, {"shop_customers", "shop_product", "shop_lead", "shop_leadline"})

    def test_meta_db_table_overrides_default(self) -> None:
        self.assertIsNone(self.registry.resolve("shop_customer"))
        self.assertIsNotNone(self.registry.resolve("shop_customers"))

    def test_abstract_model_has_no_table_but_lends_fields(self) -> None:
        self.assertIsNone(self.registry.resolve("shop_timestamped"))
        cols = {c["name"] for c in self.registry.resolve("shop_lead")["columns"]}
        self.assertIn("created_at", cols)

    def test_columns_pk_and_foreign_keys(self) -> None:
        cols = {c["name"]: c for c in self.registry.resolve("shop_lead")["columns"]}
        self.assertTrue(cols["id"]["pk"])
        self.assertEqual(cols["customer_id"]["references"], "shop_customers")
        self.assertEqual(cols["customer_id"]["type"], "ForeignKey")
        line = {c["name"]: c for c in self.registry.resolve("shop_leadline")["columns"]}
        self.assertEqual(line["lead_id"]["references"], "shop_lead")  # FK given as a string
        self.assertEqual(line["product_id"]["references"], "shop_product")


class DjangoRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph, cls.registry = scan(APP)
        cls.edges = {(e["from"], e["to"], e["kind"]): e for e in cls.graph.edges}

    def edge(self, src: str, dst: str, kind: str) -> dict:
        self.assertIn((src, dst, kind), self.edges, f"missing edge {src} -> {dst} ({kind})")
        return self.edges[(src, dst, kind)]

    def route_ids(self) -> set[str]:
        return {n["id"] for n in self.graph.nodes.values() if n["kind"] == "route"}

    def test_include_prefixes_combine(self) -> None:
        ids = self.route_ids()
        self.assertIn("route:GET:/api/leads/", ids)
        self.assertIn("route:GET:/api/customers/{pk}/", ids)
        self.assertIn("route:GET:/api/stats/customers/", ids)  # re_path regex
        self.assertFalse([i for i in ids if "admin" in i])  # admin.site.urls is not ours

    def test_api_view_verbs(self) -> None:
        ids = self.route_ids()
        self.assertIn("route:GET:/api/leads/", ids)
        self.assertIn("route:POST:/api/leads/", ids)
        self.assertNotIn("route:PUT:/api/leads/", ids)
        node = self.graph.nodes["route:POST:/api/leads/"]
        self.assertEqual(node["meta"]["handler"], "lead_list")
        self.assertNotIn("method_guess", node["meta"])
        self.assertEqual(node["meta"]["description"], "List recent leads or place a new one.")

    def test_unknown_method_is_a_guess(self) -> None:
        node = self.graph.nodes["route:GET:/api/health/"]
        self.assertTrue(node["meta"]["method_guess"])
        self.assertTrue(node["meta"]["standalone"])

    def test_method_branches_split_ops_per_verb(self) -> None:
        # GET reads through a selector; POST writes through a services.py function
        self.assertEqual(self.edge("route:GET:/api/leads/", "table:shop_lead", "read")["ops"], ["SELECT"])
        self.assertNotIn(("route:GET:/api/leads/", "table:shop_leadline", "write"), self.edges)
        self.assertEqual(self.edge("route:POST:/api/leads/", "table:shop_lead", "write")["ops"], ["INSERT"])
        self.assertEqual(self.edge("route:POST:/api/leads/", "table:shop_leadline", "write")["ops"], ["INSERT"])
        self.assertNotIn(("route:POST:/api/leads/", "table:shop_lead", "read"), self.edges)

    def test_class_based_view_verbs(self) -> None:
        ids = self.route_ids()
        self.assertIn("route:GET:/api/customers/{pk}/", ids)
        self.assertIn("route:DELETE:/api/customers/{pk}/", ids)
        self.assertNotIn("route:POST:/api/customers/{pk}/", ids)
        self.assertEqual(self.graph.nodes["route:GET:/api/customers/{pk}/"]["meta"]["handler"], "CustomerDetail.get")
        self.assertEqual(self.edge("route:GET:/api/customers/{pk}/", "table:shop_customers", "read")["ops"], ["SELECT"])
        # instance loaded with .get(), then .delete()
        ops = self.edge("route:DELETE:/api/customers/{pk}/", "table:shop_customers", "write")["ops"]
        self.assertEqual(set(ops), {"SELECT", "DELETE"})

    def test_viewset_routes_from_default_router(self) -> None:
        ids = self.route_ids()
        for node_id in (
            "route:GET:/api/products/",
            "route:POST:/api/products/",
            "route:GET:/api/products/{pk}/",
            "route:PUT:/api/products/{pk}/",
            "route:PATCH:/api/products/{pk}/",
            "route:DELETE:/api/products/{pk}/",
            "route:POST:/api/products/{pk}/restock/",  # @action(detail=True)
        ):
            self.assertIn(node_id, ids)
        self.assertEqual(self.graph.nodes["route:GET:/api/products/"]["meta"]["handler"], "ProductViewSet.list")

    def test_viewset_ops_follow_the_action(self) -> None:
        self.assertEqual(self.edge("route:GET:/api/products/", "table:shop_product", "read")["ops"], ["SELECT"])
        self.assertEqual(self.edge("route:POST:/api/products/", "table:shop_product", "write")["ops"], ["INSERT"])
        put = self.edge("route:PUT:/api/products/{pk}/", "table:shop_product", "write")["ops"]
        self.assertEqual(set(put), {"SELECT", "UPDATE"})
        delete = self.edge("route:DELETE:/api/products/{pk}/", "table:shop_product", "write")["ops"]
        self.assertEqual(set(delete), {"SELECT", "DELETE"})
        restock = self.edge("route:POST:/api/products/{pk}/restock/", "table:shop_product", "write")["ops"]
        self.assertEqual(restock, ["UPDATE"])

    def test_service_class_chain(self) -> None:
        self.edge("route:POST:/api/leads/{lead_id}/refund/", "service:RefundService", "calls")
        ops = self.edge("service:RefundService", "table:shop_lead", "write")["ops"]
        self.assertEqual(set(ops), {"SELECT", "UPDATE"})  # .get() then .save() on a loaded instance

    def test_raw_sql(self) -> None:
        edge = self.edge("route:GET:/api/stats/customers/", "table:shop_customers", "read")
        self.assertEqual(edge["ops"], ["SELECT"])

    def test_only_registry_tables_become_nodes(self) -> None:
        tables = {n["label"] for n in self.graph.nodes.values() if n["kind"] == "table"}
        self.assertTrue(tables <= {r["name"] for r in self.registry.all()})


class RegexRouteTests(unittest.TestCase):
    def test_named_groups(self) -> None:
        self.assertEqual(_regex_route(r"^items/(?P<pk>\d+)/$"), "items/{pk}/")
        self.assertEqual(_regex_route(r"^stats/customers/$"), "stats/customers/")
        self.assertEqual(_regex_route(r"^files/(?P<name>[\w.]+)\.json$"), "files/{name}.json")


if __name__ == "__main__":
    unittest.main()
