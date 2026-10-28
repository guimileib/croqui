"""StructureScanner against py_cli / js_lib / go_tool, plus the CLI fallback to it."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from croqui import cli
from croqui.adapters import ADAPTERS, detect_stacks, structure
from croqui.adapters.structure import StructureScanner
from croqui.model import Graph
from croqui.tables import build_registry

FIXTURES = Path(__file__).parent / "fixtures"
PY_CLI = FIXTURES / "py_cli"
JS_LIB = FIXTURES / "js_lib"
GO_TOOL = FIXTURES / "go_tool"


def scan(root: Path) -> tuple[Graph, StructureScanner]:
    graph = Graph({})
    scanner = StructureScanner(root, graph, build_registry(root))
    scanner.scan()
    return graph, scanner


def edge_pairs(graph: Graph) -> set[tuple[str, str]]:
    return {(e["from"], e["to"]) for e in graph.edges}


def of_kind(graph: Graph, kind: str) -> set[str]:
    return {n["id"] for n in graph.nodes.values() if n["kind"] == kind}


class DetectTests(unittest.TestCase):
    def test_any_source_project_is_detected(self) -> None:
        for root in (PY_CLI, JS_LIB, GO_TOOL):
            self.assertTrue(StructureScanner.detect(root), root.name)

    def test_empty_directory_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(StructureScanner.detect(Path(tmp)))

    def test_api_detection_never_lists_structure(self) -> None:
        self.assertNotIn("code/structure", detect_stacks(FIXTURES / "flask_app"))
        self.assertEqual(detect_stacks(PY_CLI), [])
        self.assertIn("code/structure", ADAPTERS)


class PythonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph, _ = scan(PY_CLI)

    def test_module_nodes_and_meta(self) -> None:
        node = self.graph.nodes["module:mytool/core.py"]
        self.assertEqual(node["kind"], "module")
        self.assertEqual(node["label"], "core")
        self.assertEqual(node["group"], "mytool")
        meta = node["meta"]
        self.assertEqual(meta["lang"], "python")
        self.assertEqual(meta["classes"], ["Runner"])
        self.assertEqual(meta["functions"], ["run"])  # _private is not public API
        self.assertEqual(meta["summary"], "Does the actual work.")
        self.assertGreater(meta["lines"], 5)

    def test_relative_imports_become_edges_with_evidence(self) -> None:
        self.assertIn(("module:mytool/cli.py", "module:mytool/core.py"), edge_pairs(self.graph))
        # `from . import util` names a sibling module, not an attribute of the package
        self.assertIn(("module:mytool/cli.py", "module:mytool/util.py"), edge_pairs(self.graph))
        edge = next(
            e for e in self.graph.edges
            if (e["from"], e["to"]) == ("module:mytool/core.py", "module:mytool/util.py")
        )
        self.assertEqual(edge["kind"], "calls")
        self.assertEqual(edge["evidence"], ["mytool/core.py:5"])

    def test_absolute_import_of_own_package(self) -> None:
        self.assertIn(("module:scripts/seed.py", "module:mytool/core.py"), edge_pairs(self.graph))

    def test_third_party_is_external_and_stdlib_is_not(self) -> None:
        self.assertEqual(of_kind(self.graph, "external"), {"external:requests"})
        self.assertEqual(self.graph.nodes["external:requests"]["group"], "dependências")
        self.assertIn(("module:mytool/cli.py", "external:requests"), edge_pairs(self.graph))

    def test_entries(self) -> None:
        self.assertEqual(of_kind(self.graph, "entry"), {"entry:mytool/cli.py", "entry:scripts/seed.py"})
        script = self.graph.nodes["entry:mytool/cli.py"]
        self.assertEqual(script["label"], "mytool")
        self.assertEqual(script["meta"]["how"], "script")
        self.assertEqual(script["meta"]["line"], 20)
        self.assertEqual(self.graph.nodes["entry:scripts/seed.py"]["meta"]["how"], "dir")
        self.assertIn(("entry:mytool/cli.py", "module:mytool/cli.py"), edge_pairs(self.graph))

    def test_no_self_edges(self) -> None:
        self.assertTrue(all(a != b for a, b in edge_pairs(self.graph)))

    def test_main_guard_without_declared_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run.py").write_text('import util\nif __name__ == "__main__":\n    pass\n')
            (root / "util.py").write_text("x = 1\n")
            graph, _ = scan(root)
        self.assertEqual(graph.nodes["entry:run.py"]["meta"]["how"], "main-guard")
        self.assertEqual(graph.nodes["entry:run.py"]["label"], "run")


class JavaScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph, _ = scan(JS_LIB)
        self.edges = edge_pairs(self.graph)

    def test_import_forms_resolve(self) -> None:
        # extension-less dir import -> index.js, explicit ./util/index.js, require('../src') -> src/index.js
        self.assertIn(("module:src/index.js", "module:src/util/index.js"), self.edges)
        self.assertIn(("module:src/parser.js", "module:src/util/index.js"), self.edges)
        self.assertIn(("module:bin/jslib.js", "module:src/index.js"), self.edges)
        # `export { parse } from './parser'` is an import too
        self.assertIn(("module:src/index.js", "module:src/parser.js"), self.edges)

    def test_builtins_are_not_external(self) -> None:
        self.assertEqual(of_kind(self.graph, "external"), {"external:lodash"})

    def test_package_json_entries(self) -> None:
        self.assertEqual(of_kind(self.graph, "entry"), {"entry:bin/jslib.js", "entry:src/index.js"})
        self.assertEqual(self.graph.nodes["entry:bin/jslib.js"]["meta"]["how"], "bin")
        self.assertEqual(self.graph.nodes["entry:bin/jslib.js"]["meta"]["command"], "jslib")
        self.assertEqual(self.graph.nodes["entry:src/index.js"]["meta"]["how"], "main")

    def test_meta_names(self) -> None:
        meta = self.graph.nodes["module:src/parser.js"]["meta"]
        self.assertEqual(meta["classes"], ["Parser"])
        self.assertEqual(meta["functions"], ["parse"])
        self.assertEqual(meta["summary"], "Turns text into tokens.")

    def test_index_files_are_labelled_by_directory(self) -> None:
        self.assertEqual(self.graph.nodes["module:src/util/index.js"]["label"], "util")


class GoTests(unittest.TestCase):
    def test_module_imports_and_externals(self) -> None:
        graph, _ = scan(GO_TOOL)
        self.assertIn(("module:main.go", "module:internal/store/store.go"), edge_pairs(graph))
        # fmt is std (no dot in the first element); cobra is a dependency
        self.assertEqual(of_kind(graph, "external"), {"external:github.com/spf13/cobra"})
        self.assertEqual(of_kind(graph, "entry"), {"entry:main.go"})
        self.assertEqual(graph.nodes["module:internal/store/store.go"]["meta"]["classes"], ["Store"])


class JvmTests(unittest.TestCase):
    def test_imports_resolve_to_files_and_main_is_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "src/main/java/app"
            pkg.mkdir(parents=True)
            (pkg / "Main.java").write_text(
                "package app;\nimport java.util.List;\nimport app.Util;\nimport com.google.gson.Gson;\n"
                "public class Main { public static void main(String[] a) {} }\n"
            )
            (pkg / "Util.java").write_text("package app;\npublic class Util {}\n")
            kt = root / "src/main/kotlin/tool"
            kt.mkdir(parents=True)
            (kt / "App.kt").write_text("package tool\nimport kotlin.io.println\nimport tool.Helper\nfun main() {}\n")
            (kt / "Helper.kt").write_text("package tool\nclass Helper\n")
            graph, _ = scan(root)
        edges = edge_pairs(graph)
        self.assertIn(("module:src/main/java/app/Main.java", "module:src/main/java/app/Util.java"), edges)
        self.assertIn(("module:src/main/kotlin/tool/App.kt", "module:src/main/kotlin/tool/Helper.kt"), edges)
        self.assertEqual(of_kind(graph, "external"), {"external:com.google"})
        self.assertEqual(
            of_kind(graph, "entry"),
            {"entry:src/main/java/app/Main.java", "entry:src/main/kotlin/tool/App.kt"},
        )


class DatabaseTests(unittest.TestCase):
    def test_inline_sql_wires_known_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schema.sql").write_text("CREATE TABLE leads (id int);\n")
            (root / "repo.py").write_text(
                'def q(c):\n    c.execute("INSERT INTO leads VALUES (1)")\n    c.execute("SELECT * FROM ghosts")\n'
            )
            graph, _ = scan(root)
        edge = next(e for e in graph.edges if e["to"].startswith("table:"))
        self.assertEqual(edge["from"], "module:repo.py")
        self.assertEqual(edge["kind"], "write")
        self.assertEqual(of_kind(graph, "table"), {"table:leads"})  # `ghosts` is not in the registry


class AggregationTests(unittest.TestCase):
    def test_threshold_merges_modules_per_directory(self) -> None:
        with mock.patch.object(structure, "AGGREGATE_ABOVE", 3):
            graph, _ = scan(PY_CLI)
        self.assertEqual(of_kind(graph, "module"), {"module:mytool/", "module:scripts/"})
        node = graph.nodes["module:mytool/"]
        self.assertEqual(node["meta"]["files"], 4)
        self.assertEqual(node["meta"]["lang"], "python")
        self.assertEqual(node["meta"]["summary"], "mytool: a tiny command line tool.")
        self.assertIn(("module:scripts/", "module:mytool/"), edge_pairs(graph))
        self.assertIn(("entry:mytool/cli.py", "module:mytool/"), edge_pairs(graph))
        self.assertTrue(all(a != b for a, b in edge_pairs(graph)))

    def test_externals_are_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("".join(f"import pkg{i}\n" for i in range(6)))
            with mock.patch.object(structure, "MAX_EXTERNALS", 4):
                graph, scanner = scan(root)
        self.assertEqual(len(of_kind(graph, "external")), 4)
        self.assertTrue(any("2 less-imported" in w for w in scanner.warnings))


class CliFallbackTests(unittest.TestCase):
    def run_scan(self, fixture: str, stack: str | None = None) -> tuple[int, str, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / fixture
            shutil.copytree(FIXTURES / fixture, root)
            args = argparse.Namespace(path=str(root), stack=stack, openapi=None, no_docs=True, no_migrate=True)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.cmd_scan(args)
            graph = json.loads((root / ".croqui" / "graph.json").read_text(encoding="utf-8"))
        return code, err.getvalue(), graph

    def test_non_api_project_falls_back(self) -> None:
        code, out, graph = self.run_scan("py_cli")
        self.assertEqual(code, 0)
        self.assertIn("no API framework found", out)
        self.assertIn("2 entries", out)
        self.assertIn("modules", out)
        self.assertEqual(graph["project"]["stack"], "code/structure")
        self.assertEqual([n["kind"] for n in graph["nodes"]][:2], ["entry", "entry"])  # KIND_ORDER

    def test_api_project_keeps_its_map(self) -> None:
        code, out, graph = self.run_scan("flask_app")
        self.assertEqual(code, 0)
        self.assertNotIn("code/structure", out)
        self.assertNotIn("module", graph["stats"])

    def test_forced_next_to_an_api_stack(self) -> None:
        code, _, graph = self.run_scan("flask_app", "python/flask,code/structure")
        self.assertEqual(code, 0)
        ids = [n["id"] for n in graph["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue({"route", "module"} <= set(graph["stats"]))
        known = set(ids)
        self.assertTrue(all(e["from"] in known and e["to"] in known for e in graph["edges"]))

    def test_forced_on_its_own(self) -> None:
        code, _, graph = self.run_scan("js_lib", "code/structure")
        self.assertEqual(code, 0)
        self.assertEqual(graph["stats"]["entry"], 2)

    def test_empty_project_still_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(path=tmp, stack=None, openapi=None, no_docs=True, no_migrate=True)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.cmd_scan(args), 2)


if __name__ == "__main__":
    unittest.main()
