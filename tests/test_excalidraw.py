"""`croqui draw --format excalidraw`, `croqui lint` on .excalidraw and `croqui design`."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from croqui import cli
from croqui.agents import BODY
from croqui.docs import collect_section
from croqui.draw import DRAW_MARKER, DrawError, draw, file_name, write
from croqui.lint import ERROR, WARNING, collect, lint_excalidraw
from croqui.scaffold import MARKER_WINDOW

from tests.test_draw import express_graph, flask_graph, flask_graph_obj, synthetic


def elements(drawing_text: str) -> list[dict]:
    return json.loads(drawing_text)["elements"]


def by_id(drawing_text: str) -> dict[str, dict]:
    return {el["id"]: el for el in elements(drawing_text)}


def shape(eid: str, x: float, y: float, w: float = 160, h: float = 60, kind: str = "rectangle", **extra) -> dict:
    out = {"id": eid, "type": kind, "x": x, "y": y, "width": w, "height": h, "boundElements": []}
    out.update(extra)
    return out


def text(eid: str, words: str, x: float, y: float, size: float = 16, **extra) -> dict:
    lines = words.split("\n")
    out = {"id": eid, "type": "text", "x": x, "y": y, "width": max(map(len, lines)) * size * 0.6,
           "height": len(lines) * size * 1.25, "text": words, "originalText": words,
           "fontSize": size, "fontFamily": 3, "lineHeight": 1.25, "containerId": None}
    out.update(extra)
    return out


def doc(*items: dict) -> str:
    return json.dumps({"type": "excalidraw", "version": 2, "elements": list(items)})


class ExcalidrawDrawTests(unittest.TestCase):
    def test_valid_marked_and_deterministic(self) -> None:
        drawn = draw(flask_graph(), ["POST /leads"], format="excalidraw")
        data = json.loads(drawn.text)
        self.assertEqual((data["type"], data["version"]), ("excalidraw", 2))
        self.assertIn(DRAW_MARKER, drawn.text[:MARKER_WINDOW])
        self.assertEqual(drawn.format, "excalidraw")
        self.assertEqual(drawn.text, draw(flask_graph(), ["POST /leads"], format="excalidraw").text)

    def test_design_rules_hold_for_every_element(self) -> None:
        for el in elements(draw(flask_graph(), format="excalidraw").text):
            self.assertEqual(el["roughness"], 0, el["id"])
            self.assertEqual(el["opacity"], 100, el["id"])
            if el["type"] == "text":
                self.assertEqual(el["fontFamily"], 3, el["id"])
                self.assertGreaterEqual(el["fontSize"], 13, el["id"])
                self.assertEqual(el["text"], el["originalText"])

    def test_three_zoom_levels_and_evidence(self) -> None:
        ids = by_id(draw(flask_graph(), ["POST /leads"], format="excalidraw").text)
        self.assertIn("title", ids)
        self.assertIn("→", ids["summary"]["text"])
        self.assertTrue(any(k.startswith("section-") for k in ids))
        # A table shows its real columns, in a dark code block.
        columns = [el for k, el in ids.items() if k.endswith("-columns")]
        self.assertTrue(columns)
        self.assertIn("PK", columns[0]["text"])
        self.assertEqual(ids[columns[0]["id"].replace("-columns", "-code")]["backgroundColor"], "#1e293b")
        # An endpoint is an ellipse in its verb's colour, with its handler as evidence.
        post = next(el for el in ids.values() if el["type"] == "ellipse" and el["strokeColor"] == "#1971c2")
        self.assertIn(post["id"] + "-detail", ids)

    def test_bindings_are_two_sided(self) -> None:
        ids = by_id(draw(flask_graph(), format="excalidraw").text)
        arrows = [el for el in ids.values() if el["type"] == "arrow"]
        self.assertTrue(arrows)
        for arrow in arrows:
            for end in ("startBinding", "endBinding"):
                target = ids[arrow[end]["elementId"]]
                self.assertIn({"type": "arrow", "id": arrow["id"]}, target["boundElements"])
        for el in ids.values():
            if el["type"] == "text" and el.get("containerId"):
                self.assertIn({"type": "text", "id": el["id"]}, ids[el["containerId"]]["boundElements"])

    def test_overview_groups_endpoints_and_a_flow_keeps_them(self) -> None:
        overview = by_id(draw(flask_graph(), format="excalidraw").text)
        self.assertFalse([el for el in overview.values() if el["type"] == "ellipse" and el["strokeStyle"] == "solid"])
        listed = "\n".join(el["text"] for k, el in overview.items() if k.endswith("-columns"))
        self.assertIn("/leads/", listed)
        flow = by_id(draw(flask_graph(), ["POST /leads"], format="excalidraw").text)
        self.assertTrue([el for el in flow.values() if el["type"] == "ellipse" and el["strokeStyle"] == "solid"])

    def test_lint_clean_by_construction(self) -> None:
        for graph in (flask_graph(), express_graph(), synthetic(12, 15)):
            for targets in ([], ["leads"], ["/leads"], ["S1"]):
                for direction in ("LR", "TB"):
                    try:
                        drawn = draw(graph, targets, format="excalidraw", direction=direction)
                    except DrawError:
                        continue
                    problems = [i for i in lint_excalidraw(drawn.text, "x.excalidraw") if i.level != "info"]
                    self.assertEqual(problems, [], f"{targets} {direction}")

    def test_file_names_follow_the_format(self) -> None:
        self.assertEqual(file_name(None, "leads", "excalidraw"), "leads.excalidraw")
        self.assertEqual(file_name("fluxo", "x", "excalidraw"), "fluxo.excalidraw")
        with self.assertRaises(DrawError):
            file_name("fluxo.mmd", "x", "excalidraw")
        with self.assertRaises(DrawError):
            file_name("fluxo.excalidraw", "x", "mermaid")
        with self.assertRaises(DrawError):
            draw(flask_graph(), format="svg")

    def test_writes_rewrites_and_respects_a_file_made_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drawn = draw(flask_graph(), ["leads"], format="excalidraw")
            path, status = write(root, drawn)
            self.assertEqual((path.name, status), ("leads.excalidraw", "criado"))
            self.assertEqual(write(root, drawn)[1], "igual")
            path.write_text(path.read_text(encoding="utf-8").replace(DRAW_MARKER, "meu"), encoding="utf-8")
            with self.assertRaises(DrawError):
                write(root, drawn)


class ExcalidrawLintTests(unittest.TestCase):
    def found(self, data: str) -> list[tuple[str, str]]:
        return [(i.level, i.message) for i in lint_excalidraw(data, "d.excalidraw")]

    def test_not_json_or_not_excalidraw(self) -> None:
        self.assertEqual(self.found("{nope")[0][0], ERROR)
        self.assertEqual(self.found('{"type": "x", "elements": []}')[0][0], ERROR)
        self.assertEqual(self.found('{"type": "excalidraw", "elements": []}')[0][0], ERROR)

    def test_repeated_ids_are_errors(self) -> None:
        issues = self.found(doc(shape("a", 0, 0), shape("a", 0, 0)))
        self.assertIn((ERROR, "id `a` aparece 2 vezes — colado, cada cópia vira um desenho a mais"), issues)

    def test_broken_and_one_sided_bindings(self) -> None:
        orphan = text("t", "oi", 0, 0, containerId="ghost")
        one_sided = text("u", "oi", 10, 10, containerId="box")
        arrow = {"id": "ar", "type": "arrow", "x": 0, "y": 0, "width": 10, "height": 0, "points": [[0, 0], [10, 0]],
                 "startBinding": {"elementId": "nowhere"}}
        issues = self.found(doc(shape("box", 0, 0), orphan, one_sided, arrow))
        levels = sorted({level for level, _ in issues})
        self.assertEqual(levels, [ERROR, WARNING])
        self.assertTrue(any("ghost" in m for _, m in issues))
        self.assertTrue(any("nowhere" in m for _, m in issues))
        self.assertTrue(any("não lista o texto" in m for _, m in issues))

    def test_writing_that_does_not_fit(self) -> None:
        box = shape("box", 0, 0, 120, 40, boundElements=[{"type": "text", "id": "t"}])
        long = text("t", "uma frase comprida que não cabe nessa caixa pequena", 0, 0, containerId="box")
        self.assertTrue(any("não cabe" in m for _, m in self.found(doc(box, long))))
        wide = text("t", "palavraenormequenaocabe", 0, 0, containerId="box")
        self.assertTrue(any("mais larga" in m for _, m in self.found(doc(box, wide))))

    def test_overlaps_but_not_containment(self) -> None:
        issues = self.found(doc(shape("a", 0, 0), shape("b", 100, 20)))
        self.assertTrue(any("se sobrepõem" in m for _, m in issues))
        self.assertEqual(self.found(doc(shape("a", 0, 0, 400, 300), shape("b", 20, 20))), [])
        self.assertTrue(any("por cima" in m for _, m in self.found(doc(shape("a", 0, 0), text("t", "solto", 140, 20)))))

    def test_arrow_stopping_short(self) -> None:
        a = shape("a", 0, 0, boundElements=[{"type": "arrow", "id": "ar"}])
        b = shape("b", 600, 0, boundElements=[{"type": "arrow", "id": "ar"}])
        arrow = {"id": "ar", "type": "arrow", "x": 164, "y": 30, "width": 200, "height": 0,
                 "points": [[0, 0], [200, 0]], "startBinding": {"elementId": "a"}, "endBinding": {"elementId": "b"}}
        self.assertTrue(any("para a" in m for _, m in self.found(doc(a, b, arrow))))

    def test_lint_and_docs_pick_up_drawings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, draw(flask_graph(), ["leads"], format="excalidraw"))
            self.assertEqual([p.name for p in collect(root)], ["leads.excalidraw"])
            docs = collect_section(root, "uml")
            self.assertEqual([(d["format"], d["title"]) for d in docs],
                             [("excalidraw", draw(flask_graph(), ["leads"]).title)])


class ExcalidrawCliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_draw_then_lint_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flask_graph_obj().write(root / ".croqui" / "graph.json")
            code, _, err = self.run_cli("draw", str(root), "/leads", "--format", "excalidraw")
            self.assertEqual(code, 0)
            self.assertIn("leads.excalidraw", err)
            code, out, _ = self.run_cli("lint", "--path", str(root))
            self.assertEqual((code, out.strip().splitlines()[-1]), (0, "1 arquivo(s): 0 erro(s), 0 aviso(s), 0 info"))

    def test_design_prints_the_method_and_the_skill_points_at_it(self) -> None:
        code, out, _ = self.run_cli("design")
        self.assertEqual(code, 0)
        for words in ("isomorphism", "Three zoom levels", "croqui lint", "#1e293b", "containerId"):
            self.assertIn(words, out)
        self.assertIn("croqui design", BODY)
        self.assertIn("--format excalidraw", BODY)


if __name__ == "__main__":
    unittest.main()
