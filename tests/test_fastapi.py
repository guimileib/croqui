"""FastApiScanner against SQLAlchemy Core tables in tests/fixtures/fastapi_core."""

from __future__ import annotations

import unittest
from pathlib import Path

from croqui.adapters.fastapi_py import FastApiScanner
from croqui.model import Graph
from croqui.tables import build_registry

APP = Path(__file__).parent / "fixtures" / "fastapi_core"


def scan(root: Path):
    registry = build_registry(root)
    FastApiScanner.register_tables(root, registry)
    graph = Graph({})
    FastApiScanner(root, graph, registry).scan()
    return graph, registry


class FastApiCoreTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph, cls.registry = scan(APP)
        cls.edges = {(e["from"], e["to"], e["kind"]): e for e in cls.graph.edges}

    def edge(self, src: str, dst: str, kind: str) -> dict:
        self.assertIn((src, dst, kind), self.edges, f"missing edge {src} -> {dst} ({kind})")
        return self.edges[(src, dst, kind)]

    def test_core_tables_take_the_metadata_schema(self) -> None:
        self.assertEqual(self.registry.qualified("tb_agent"), "core.tb_agent")
        self.assertEqual(self.registry.qualified("tb_event"), "core.tb_event")

    def test_a_table_from_another_library_is_not_a_database_object(self) -> None:
        self.assertIsNone(self.registry.resolve("Name"))

    def test_statement_built_on_a_table_sets_its_operation(self) -> None:
        edge = self.edge("repo:AgentsRepository", "table:core.tb_agent", "write")
        self.assertEqual(edge["ops"], ["INSERT", "SELECT", "UPDATE"])
        self.assertNotIn("unresolved", self.graph.nodes["repo:AgentsRepository"]["meta"])

    def test_a_column_reference_is_a_read(self) -> None:
        edge = self.edge("repo:EventsRepository", "table:core.tb_event", "read")
        self.assertEqual(edge["ops"], ["SELECT"])

    def test_f_string_sql_fills_in_project_constants(self) -> None:
        edge = self.edge("repo:StatusRepository", "table:core.tb_agent", "read")
        self.assertEqual(edge["ops"], ["SELECT"])

    def test_alembic_version_table_takes_the_configured_schema(self) -> None:
        self.edge("repo:SchemaRepository", "table:core.alembic_version", "read")


if __name__ == "__main__":
    unittest.main()
