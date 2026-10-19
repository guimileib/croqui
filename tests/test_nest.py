"""NestScanner against tests/fixtures/nest_app."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from croqui.adapters._js import OrmIndex
from croqui.adapters.nestjs_ts import NestScanner
from croqui.model import Graph
from croqui.tables import TableRegistry, build_registry

APP = Path(__file__).parent / "fixtures" / "nest_app"


def scan(root: Path) -> tuple[Graph, TableRegistry]:
    registry = build_registry(root, code_suffixes=(".ts",))
    NestScanner.register_tables(root, registry)
    graph = Graph({"name": root.name})
    NestScanner(root, graph, registry).scan()
    return graph, registry


class NestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph, cls.registry = scan(APP)
        cls.edges = {(e["from"], e["to"], e["kind"]): e for e in cls.graph.edges}

    def test_routes_have_global_prefix(self) -> None:
        routes = {n for n in self.graph.nodes if n.startswith("route:")}
        self.assertEqual(
            routes,
            {
                "route:GET:/api/users",
                "route:GET:/api/users/{id}",
                "route:POST:/api/users",
                "route:PUT:/api/users/{id}",
                "route:DELETE:/api/users/{id}",
                "route:GET:/api/orders",
                "route:POST:/api/orders",
                "route:GET:/api/orders/{id}/items",
            },
        )
        self.assertFalse([n for n in self.graph.nodes if "legacy" in n])  # commented out

    def test_route_meta(self) -> None:
        node = self.graph.nodes["route:GET:/api/users"]
        meta = node["meta"]
        self.assertEqual(meta["summary"], "List users")
        self.assertEqual(meta["description"], "Returns every user.")
        self.assertEqual(meta["handler"], "findAll")
        self.assertEqual(meta["source"], "src/users/users.controller.ts:13")
        self.assertEqual(node["group"], "Users")
        self.assertEqual(self.graph.nodes["route:POST:/api/users"]["meta"]["request_body"], "CreateUserDto")

    def test_typeorm_service_goes_straight_to_table(self) -> None:
        for rid in ("route:GET:/api/users", "route:DELETE:/api/users/{id}"):
            self.assertIn((rid, "service:UsersService", "calls"), self.edges)
        edge = self.edges[("service:UsersService", "table:core.app_users", "write")]
        self.assertEqual(edge["ops"], ["DELETE", "INSERT", "SELECT", "UPDATE"])
        self.assertNotIn("repo:UsersService", self.graph.nodes)

    def test_entity_name_schema_and_columns(self) -> None:
        rec = self.registry.resolve("app_users")
        self.assertEqual(rec["schema"], "core")
        cols = {c["name"]: c for c in rec["columns"]}
        self.assertIn("email_address", cols)
        self.assertTrue(cols["id"]["pk"])

    def test_custom_repository_prisma_chain(self) -> None:
        self.assertIn(("route:POST:/api/orders", "service:OrdersService", "calls"), self.edges)
        self.assertIn(("service:OrdersService", "repo:OrdersRepository", "calls"), self.edges)
        edge = self.edges[("repo:OrdersRepository", "table:orders", "write")]
        self.assertEqual(edge["ops"], ["INSERT", "SELECT"])  # prisma.order.findUnique / create
        self.assertNotIn("service:PrismaService", self.graph.nodes)

    def test_only_used_injected_services_are_linked(self) -> None:
        # OrdersController injects AuditService but never uses it
        self.assertFalse([k for k in self.edges if k[1] == "service:AuditService"])
        health = self.graph.nodes["route:GET:/api/orders"]["meta"]
        self.assertTrue(health["standalone"])
        self.assertFalse([k for k in self.edges if k[0] == "route:GET:/api/orders"])

    def test_stack_and_prisma_map(self) -> None:
        self.assertEqual(OrmIndex.build(APP).prisma, {"order": "orders"})
        self.assertEqual(NestScanner.stack, "node/nestjs")


class NestEdgeCases(unittest.TestCase):
    def test_versioning_and_object_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"dependencies": {"@nestjs/core": "10"}}', encoding="utf-8")
            (root / "main.ts").write_text(
                "app.setGlobalPrefix('api');\napp.enableVersioning({ type: VersioningType.URI, defaultVersion: '1' });\n",
                encoding="utf-8",
            )
            (root / "cats.controller.ts").write_text(
                "@Controller({ path: ['cats', 'felines'] })\nexport class CatsController {\n"
                "  @Get(':id') one() { return 1 }\n  @Version('2') @Post() add() { return 2 }\n}\n",
                encoding="utf-8",
            )
            graph, _ = scan(root)
            self.assertEqual(
                {n for n in graph.nodes if n.startswith("route:")},
                {
                    "route:GET:/api/v1/cats/{id}",
                    "route:GET:/api/v1/felines/{id}",
                    "route:POST:/api/v2/cats",
                    "route:POST:/api/v2/felines",
                },
            )


if __name__ == "__main__":
    unittest.main()
