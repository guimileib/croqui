"""`croqui draw`: a styled flowchart from the map, deterministic and valid by construction."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from croqui import cli
from croqui.adapters.express_js import ExpressScanner
from croqui.adapters.flask_py import FlaskScanner
from croqui.draw import DRAW_MARKER, DrawError, draw, file_name, resolve, write
from croqui.lint import lint_text
from croqui.model import Graph
from croqui.scaffold import MARKER
from croqui.tables import build_registry

FIXTURES = Path(__file__).parent / "fixtures"


def flask_graph_obj() -> Graph:
    root = FIXTURES / "flask_app"
    registry = build_registry(root)
    FlaskScanner.register_tables(root, registry)
    graph = Graph({"name": "flask_app"})
    FlaskScanner(root, graph, registry).scan()
    graph.drop_dangling_edges()
    return graph


def flask_graph() -> dict:
    return flask_graph_obj().to_dict()


def express_graph() -> dict:
    root = FIXTURES / "express_app"
    registry = build_registry(root, code_suffixes=(".js", ".ts"))
    ExpressScanner.register_tables(root, registry)
    graph = Graph({"name": "express_app"})
    ExpressScanner(root, graph, registry).scan()
    graph.drop_dangling_edges()
    return graph.to_dict()


def synthetic(routes: int, services: int) -> dict:
    """Every route calls every service, each service writes its own table: far above any cap."""
    nodes = [{"id": f"route:GET:/r{i}", "kind": "route", "label": f"GET /r{i}", "group": f"g{i % 3}",
              "meta": {"method": "GET", "path": f"/r{i}"}} for i in range(routes)]
    nodes += [{"id": f"service:S{j}", "kind": "service", "label": f"S{j}", "group": None, "meta": {}}
              for j in range(services)]
    nodes += [{"id": f"table:t{j}", "kind": "table", "label": f"t{j}", "group": None, "meta": {}}
              for j in range(services)]
    edges = [{"from": f"route:GET:/r{i}", "to": f"service:S{j}", "kind": "calls", "ops": []}
             for i in range(routes) for j in range(services)]
    edges += [{"from": f"service:S{j}", "to": f"table:t{j}", "kind": "write", "ops": ["INSERT"]}
              for j in range(services)]
    return {"project": {"name": "big"}, "nodes": nodes, "edges": edges}


def node_lines(text: str) -> list[str]:
    arrows = ("-->", "==>", "-.->")
    return [ln for ln in text.splitlines() if ":::" in ln and not any(a in ln for a in arrows)]


class DrawContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = flask_graph()

    def test_overview_is_styled_and_marked(self) -> None:
        text = draw(self.graph).text
        self.assertTrue(text.startswith("---\ntitle: Visão geral — flask_app\n---\n"))
        self.assertIn(f"%% {DRAW_MARKER}", text)
        self.assertNotIn(MARKER, text)
        self.assertIn("flowchart LR", text)
        for frame in ('["API"]', '["domínio"]', '["repositórios"]', '["dados"]', '["externos"]'):
            self.assertIn(frame, text)
        for cls in (":::get", ":::post", ":::delete", ":::service", ":::repo", ":::table", ":::external"):
            self.assertIn(cls, text)

    def test_shapes_per_kind(self) -> None:
        text = draw(self.graph).text
        self.assertIn('r1(["', text)
        self.assertIn('s1["LeadService"]:::service', text)
        self.assertIn('p1[["LeadRepository"]]:::repo', text)
        self.assertIn('[("leads")]:::table', text)
        self.assertIn('{{"HTTP / Webhook"}}:::external', text)

    def test_edges_use_ops_and_arrow_style(self) -> None:
        text = draw(self.graph, ["POST /leads"]).text
        self.assertIn("==>|grava: delete/insert|", text)          # the repo writes the table
        self.assertIn("-->|envia|", text)                        # service -> external
        self.assertIn("-.->|lê|", draw(self.graph, ["/reports/revenue"]).text)

    def test_deterministic(self) -> None:
        for targets in ([], ["POST /leads"], ["leads", "user"]):
            self.assertEqual(draw(self.graph, targets).text, draw(self.graph, targets).text)
        shuffled = dict(self.graph, nodes=list(reversed(self.graph["nodes"])),
                        edges=list(reversed(self.graph["edges"])))
        self.assertEqual(draw(self.graph).text, draw(shuffled).text)

    def test_target_chain_goes_down_to_the_table_and_one_level_up(self) -> None:
        text = draw(self.graph, ["LeadService"]).text
        self.assertIn('"LeadRepository"', text)
        self.assertIn('("leads")', text)
        self.assertIn('"GET /leads/"', text)                      # who calls it
        self.assertNotIn("/api/users", text)                      # unrelated

    def test_depth_zero_keeps_only_the_target_and_its_callers(self) -> None:
        text = draw(self.graph, ["LeadService"], depth=0).text
        self.assertNotIn("LeadRepository", text)
        self.assertIn('"GET /leads/"', text)

    def test_direction_and_title(self) -> None:
        drawn = draw(self.graph, ["leads"], direction="tb", title="Meu fluxo")
        self.assertIn("flowchart TB", drawn.text)
        self.assertIn("title: Meu fluxo", drawn.text)
        with self.assertRaises(DrawError):
            draw(self.graph, direction="XY")


class ResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = flask_graph()
        cls.nodes = {n["id"]: n for n in cls.graph["nodes"]}

    def labels(self, needle: str) -> list[str]:
        return [n["label"] for n in resolve(self.nodes, needle)]

    def test_bare_path_matches_every_method_and_ignores_trailing_slash(self) -> None:
        self.assertEqual(self.labels("/leads"), ["GET /leads/", "POST /leads/"])
        self.assertEqual(self.labels("POST /leads"), ["POST /leads/"])

    def test_id_table_service_and_case(self) -> None:
        self.assertEqual(self.labels("route:POST:/leads/"), ["POST /leads/"])
        self.assertEqual(self.labels("LEADS"), ["leads"])
        self.assertEqual(self.labels("leadservice"), ["LeadService"])

    def test_group_then_fragment(self) -> None:
        self.assertEqual(len(self.labels("reports")), 3)           # the group
        self.assertEqual(self.labels("revenue"), ["GET /reports/revenue"])
        self.assertEqual(self.labels("zz-nothing"), [])

    def test_unknown_target_is_an_error_and_partial_is_reported(self) -> None:
        with self.assertRaises(DrawError):
            draw(self.graph, ["nao-existe"])
        drawn = draw(self.graph, ["leads", "nao-existe"])
        self.assertEqual(drawn.missing, ["nao-existe"])


class DrawLimitsTests(unittest.TestCase):
    def test_max_nodes_caps_overview_and_target(self) -> None:
        graph = synthetic(routes=12, services=15)
        for cap in (5, 12, 25):
            drawn = draw(graph, max_nodes=cap)
            self.assertLessEqual(drawn.nodes, cap)
            self.assertEqual(len(node_lines(drawn.text)), drawn.nodes)
            self.assertGreater(drawn.omitted, 0)
        text = draw(graph, ["S1"], max_nodes=6).text
        self.assertEqual(len(node_lines(text)), 6)
        self.assertRegex(text, r"%% \d+ nós ficaram de fora")

    def test_nothing_left_out_says_nothing(self) -> None:
        self.assertNotIn("ficaram de fora", draw(flask_graph()).text)

    def test_one_chain_bigger_than_the_cap_still_draws(self) -> None:
        self.assertEqual(draw(synthetic(routes=1, services=30), max_nodes=10).nodes, 10)

    def test_empty_graph(self) -> None:
        with self.assertRaises(DrawError):
            draw({"nodes": [], "edges": []})


class DrawKindsTests(unittest.TestCase):
    def graph(self) -> dict:
        return {
            "project": {"name": "mix"},
            "nodes": [
                {"id": "entry:main", "kind": "entry", "label": "main()", "group": None, "meta": {}},
                {"id": "module:billing", "kind": "module", "label": "billing", "group": None, "meta": {}},
                {"id": "thing:x", "kind": "gizmo", "label": 'odd "name" -- 100%% | x', "group": None, "meta": {}},
            ],
            "edges": [
                {"from": "entry:main", "to": "module:billing", "kind": "calls", "ops": []},
                {"from": "module:billing", "to": "thing:x", "kind": "calls", "ops": []},
            ],
        }

    def test_entry_and_module_and_unknown_kinds(self) -> None:
        text = draw(self.graph()).text
        self.assertIn('e1(["main()"]):::accent', text)
        self.assertIn('m1["billing"]:::service', text)
        self.assertIn(":::muted", text)
        self.assertIn('["API"]', text)
        self.assertIn('["domínio"]', text)

    def test_labels_are_escaped(self) -> None:
        body = draw(self.graph()).text.split("flowchart", 1)[1]
        self.assertNotIn("--", body.replace("-->", ""))
        self.assertNotIn("%%", body)
        self.assertNotIn("|", "\n".join(node_lines(body)))

    def test_valid_by_construction(self) -> None:
        for graph in (self.graph(), flask_graph(), express_graph(), synthetic(12, 15)):
            for targets in ([], ["leads"], ["/leads"], ["S1"]):
                try:
                    drawn = draw(graph, targets)
                except DrawError:
                    continue
                self.assertEqual(lint_text(drawn.text, "x.mmd"), [], f"{targets}: {drawn.text}")


class DrawWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.drawing = draw(flask_graph(), ["leads"])

    def uml(self, name: str) -> Path:
        return self.root / ".croqui" / "uml" / name

    def test_writes_then_updates_its_own_file(self) -> None:
        path, status = write(self.root, self.drawing)
        self.assertEqual((path, status), (self.uml("leads.mmd"), "criado"))
        self.assertEqual(write(self.root, self.drawing)[1], "igual")
        again = draw(flask_graph(), ["leads"], title="Outro")
        self.assertEqual(write(self.root, again)[1], "atualizado")
        self.assertIn("title: Outro", path.read_text(encoding="utf-8"))

    def test_uses_the_projects_own_uml_folder(self) -> None:
        (self.root / "docs" / "uml").mkdir(parents=True)
        path, _ = write(self.root, self.drawing)
        self.assertEqual(path, self.root / "docs" / "uml" / "leads.mmd")

    def test_refuses_a_foreign_file_unless_forced(self) -> None:
        self.uml("leads.mmd").parent.mkdir(parents=True)
        self.uml("leads.mmd").write_text("flowchart LR\n  A --> B\n", encoding="utf-8")
        with self.assertRaises(DrawError):
            write(self.root, self.drawing)
        self.assertIn("A --> B", self.uml("leads.mmd").read_text(encoding="utf-8"))
        write(self.root, self.drawing, force=True)
        self.assertIn(DRAW_MARKER, self.uml("leads.mmd").read_text(encoding="utf-8"))

    def test_never_overwrites_generated_files_even_forced(self) -> None:
        self.uml("x.mmd").parent.mkdir(parents=True)
        self.uml("x.mmd").write_text(f"%% {MARKER} — reescrito\nflowchart LR\n  A --> B\n", encoding="utf-8")
        for force in (False, True):
            with self.assertRaises(DrawError):
                write(self.root, self.drawing, name="x", force=force)
            with self.assertRaises(DrawError):
                write(self.root, self.drawing, name="relacoes", force=force)
        self.assertNotIn(DRAW_MARKER, self.uml("x.mmd").read_text(encoding="utf-8"))

    def test_file_names(self) -> None:
        self.assertEqual(file_name(None, "leads"), "leads.mmd")
        self.assertEqual(file_name("fluxo", "x"), "fluxo.mmd")
        self.assertEqual(file_name("sub/dir/fluxo.mermaid", "x"), "fluxo.mermaid")
        with self.assertRaises(DrawError):
            file_name(".hidden", "x")
        self.assertEqual(draw(flask_graph(), ["POST /leads"]).name, "post-leads")
        self.assertEqual(draw(flask_graph()).name, "visao-geral")


class DrawCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        flask_graph_obj().write(self.root / ".croqui" / "graph.json")

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_stdout_with_explicit_path(self) -> None:
        code, out, _ = self.run_cli("draw", str(self.root), "POST /leads", "--stdout")
        self.assertEqual(code, 0)
        self.assertIn("flowchart LR", out)
        self.assertFalse((self.root / ".croqui" / "uml").exists())

    def test_writes_and_lints_clean(self) -> None:
        code, _, err = self.run_cli("draw", str(self.root), "/leads", "--name", "leads-api")
        self.assertEqual(code, 0)
        self.assertIn("leads-api.mmd", err)
        code, out, _ = self.run_cli("lint", "--path", str(self.root))
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "1 arquivo(s): 0 erro(s), 0 aviso(s), 0 info"))

    def test_first_word_is_a_path_only_when_it_holds_a_graph(self) -> None:
        # `leads` is a folder here, but with no graph inside it is a target, not a project
        (self.root / "leads").mkdir()
        old = Path.cwd()
        import os
        os.chdir(self.root)
        try:
            code, out, _ = self.run_cli("draw", "leads", "--stdout")
        finally:
            os.chdir(old)
        self.assertEqual(code, 0)
        self.assertIn('("leads")', out)

    def test_errors_exit_2(self) -> None:
        self.assertEqual(self.run_cli("draw", str(self.root), "nao-existe")[0], 2)
        self.assertEqual(self.run_cli("draw", str(self.root), "--max-nodes", "0")[0], 2)


class DrawMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        flask_graph_obj().write(self.root / ".croqui" / "graph.json")

    def call(self, tool: str, **arguments) -> tuple[str, bool]:
        from croqui.mcp import handle

        reply = handle(self.root, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}})
        result = reply["result"]
        return result["content"][0]["text"], result["isError"]

    def test_tools_are_listed(self) -> None:
        from croqui.mcp import TOOLS

        names = {t["name"] for t in TOOLS}
        self.assertTrue({"croqui_draw", "croqui_lint"} <= names)

    def test_draw_then_lint(self) -> None:
        text, failed = self.call("croqui_draw", targets=["/leads"])
        self.assertFalse(failed)
        self.assertIn(".croqui/uml/leads.mmd", text)
        text, failed = self.call("croqui_lint")
        self.assertFalse(failed)
        self.assertIn("1 arquivo(s): 0 erro(s), 0 aviso(s), 0 info", text)

    def test_preview_writes_nothing_and_refusals_are_tool_errors(self) -> None:
        text, failed = self.call("croqui_draw", targets="leads", preview=True)
        self.assertFalse(failed)
        self.assertIn("flowchart LR", text)
        self.assertFalse((self.root / ".croqui" / "uml").exists())
        for arguments in ({"targets": ["nao-existe"]}, {"name": "relacoes"}, {"depth": "x"}):
            self.assertTrue(self.call("croqui_draw", **arguments)[1], arguments)
        self.assertTrue(self.call("croqui_lint", files=["../fora.mmd"])[1])


if __name__ == "__main__":
    unittest.main()
