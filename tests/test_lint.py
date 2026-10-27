"""`croqui lint`: a static mirror of what viewer/diagram.js draws."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from croqui import cli
from croqui.lint import ERROR, INFO, WARNING, collect, lint_paths, lint_text

GOOD = """---
title: Leads — POST /leads
---
flowchart LR
  subgraph api["API"]
    r1([POST /leads]):::post
  end
  subgraph data["dados"]
    t1[(tb_leads)]:::table
  end
  classDef hot fill:#ffe3e3,stroke:#c92a2a,color:#5c0000
  s1[LeadService]:::hot
  r1 ==>|grava: insert| s1
  s1 -.->|lê| t1
"""


def levels(text: str) -> list[tuple[int, str]]:
    return [(i.line, i.level) for i in lint_text(text, "d.mmd")]


def messages(text: str) -> str:
    return "\n".join(i.message for i in lint_text(text, "d.mmd"))


class LintFlowTests(unittest.TestCase):
    def test_good_file_is_clean(self) -> None:
        self.assertEqual(lint_text(GOOD, "d.mmd"), [])

    def test_issue_format(self) -> None:
        issue = lint_text("flowchart LR\n  end\n  A --> B\n", "uml/d.mmd")[1]
        self.assertEqual(str(issue), "uml/d.mmd:2: error: `end` sem `subgraph` aberto")

    def test_unbalanced_subgraph_is_an_error(self) -> None:
        text = GOOD.replace("  end\n", "", 1)
        found = [(i.line, i.message) for i in lint_text(text, "d.mmd") if i.level == ERROR]
        self.assertEqual(found, [(5, "`subgraph` sem `end`")])

    def test_stray_end_is_an_error(self) -> None:
        self.assertIn((2, ERROR), levels("flowchart LR\n  end\n  A --> B\n"))

    def test_unknown_class_warns_but_palette_and_classdef_do_not(self) -> None:
        text = "---\ntitle: t\n---\nflowchart LR\n  A[a]:::nope\n  B[b]:::table\n  class A,B tbale\n"
        warn = [i for i in lint_text(text, "d.mmd") if i.level == WARNING]
        self.assertEqual([i.line for i in warn], [5, 7])
        self.assertIn("`nope`", warn[0].message)
        self.assertIn("`tbale`", warn[1].message)

    def test_classdef_defined_after_use_counts(self) -> None:
        text = "---\ntitle: t\n---\nflowchart LR\n  A[a]:::hot\n  classDef hot fill:#fee\n"
        self.assertEqual(lint_text(text, "d.mmd"), [])

    def test_unsupported_diagram_type_warns(self) -> None:
        got = lint_text("---\ntitle: t\n---\nclassDiagram\n  A <|-- B\n", "d.mmd")
        self.assertEqual([(i.line, i.level) for i in got], [(4, WARNING)])
        self.assertIn("classDiagram", got[0].message)
        self.assertIn("código", got[0].message)

    def test_too_many_nodes_and_long_labels(self) -> None:
        many = "flowchart LR\n" + "".join(f"  n{i}[x{i}]\n" for i in range(26))
        self.assertIn("26 nós", messages(many))
        wordy = "flowchart LR\n  A[um dois tres quatro cinco seis sete] --> B\n"
        self.assertIn((2, INFO), levels(wordy))

    def test_ignored_directives_are_info(self) -> None:
        text = "---\ntitle: t\n---\nflowchart LR\n  A --> B\n  linkStyle 0 stroke:red\n  click A callback\n"
        self.assertEqual(levels(text), [(6, INFO), (7, INFO)])

    def test_missing_title_is_info(self) -> None:
        self.assertEqual(levels("flowchart LR\n  A --> B\n"), [(1, INFO)])

    def test_glued_arrow_and_trailing_junk(self) -> None:
        self.assertIn("engole a seta", messages("---\ntitle: t\n---\nflowchart LR\n  A-->B\n"))
        self.assertIn("texto ignorado", messages("---\ntitle: t\n---\nflowchart LR\n  A[a] --> B[b] sobrou\n"))
        self.assertIn("aresta sem destino", messages("---\ntitle: t\n---\nflowchart LR\n  A --> \n"))

    def test_obvious_id_typo_is_info_and_vague_ones_stay_quiet(self) -> None:
        typo = "---\ntitle: t\n---\nflowchart LR\n  svc1[Service]\n  svc1 --> repo1[Repo]\n  svc2 --> repo1\n"
        self.assertIn("`svc2`", messages(typo))
        quiet = "---\ntitle: t\n---\nflowchart LR\n  A --> B\n  B --> C\n"
        self.assertEqual(lint_text(quiet, "d.mmd"), [])

    def test_fence_comments_and_init_are_ignored(self) -> None:
        text = ("```mermaid\n---\ntitle: t\n---\n%%{init: {}}%%\n%% a comment\nflowchart TD\n  A --> B\n```\n")
        self.assertEqual(lint_text(text, "d.mmd"), [])

    def test_empty_file(self) -> None:
        self.assertEqual(levels("  \n"), [(1, WARNING)])


class LintSequenceTests(unittest.TestCase):
    def test_clean_sequence(self) -> None:
        text = ("---\ntitle: t\n---\nsequenceDiagram\n  participant A as App\n"
                "  A->>B: pede\n  B-->>A: responde\n  Note over A,B: ok\n")
        self.assertEqual(lint_text(text, "d.mmd"), [])

    def test_blocks_are_info_and_garbage_is_a_warning(self) -> None:
        text = ("---\ntitle: t\n---\nsequenceDiagram\n  A->>B: x\n  loop cada 5s\n    B->>A: y\n  end\n"
                "  isto nao e uma linha\n")
        self.assertEqual(levels(text), [(6, INFO), (9, WARNING)])


class LintFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.uml = self.root / ".croqui" / "uml"
        self.uml.mkdir(parents=True)

    def put(self, name: str, text: str) -> Path:
        path = self.uml / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_default_is_the_uml_folder_and_generated_files_are_skipped(self) -> None:
        self.put("ok.mmd", GOOD)
        self.put("relacoes.mmd", "%% croqui:gerado — x\nflowchart LR\n" + "".join(f"  n{i}[x]\n" for i in range(60)))
        self.put("notes.md", "# not a diagram")
        self.assertEqual([p.name for p in collect(self.root)], ["ok.mmd", "relacoes.mmd"])
        issues, checked = lint_paths(self.root)
        self.assertEqual((issues, checked), ([], 1))

    def test_exit_code_and_json(self) -> None:
        self.put("bad.mmd", "flowchart LR\n  subgraph a\n    A --> B\n")
        code, out = self.run_cli("lint", "--path", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("bad.mmd:2: error: `subgraph` sem `end`", out)
        code, out = self.run_cli("lint", "--path", str(self.root), "--json")
        data = json.loads(out)
        self.assertEqual((code, data["errors"], data["files"]), (1, 1, 1))
        self.assertEqual([i["line"] for i in data["issues"] if i["level"] == "error"], [2])

    def test_warnings_alone_exit_zero_and_missing_file_exits_two(self) -> None:
        self.put("w.mmd", "---\ntitle: t\n---\nclassDiagram\n  A <|-- B\n")
        self.assertEqual(self.run_cli("lint", "--path", str(self.root))[0], 0)
        self.assertEqual(self.run_cli("lint", str(self.root / "nao-existe.mmd"))[0], 2)

    def test_explicit_files(self) -> None:
        path = self.put("ok.mmd", GOOD)
        code, out = self.run_cli("lint", str(path), "--path", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("1 arquivo(s): 0 erro(s), 0 aviso(s), 0 info", out)


if __name__ == "__main__":
    unittest.main()
