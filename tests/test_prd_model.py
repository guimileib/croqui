"""The "modelagem PRD" tab: the erDiagram a PRD carries, lifted onto its own stage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from croqui.docs import collect_docs, prd_models

PRD = """# PRD — Loja

## Problema
Texto.

```mermaid
flowchart LR
  a --> b
```

## Modelo de dados

```mermaid
erDiagram
  CLIENTE ||--o{ PEDIDO : faz
  PEDIDO {
    int id PK
    int cliente_id FK
  }
```

Cada pedido é de um cliente.
"""


class PrdModelTests(unittest.TestCase):
    def test_only_er_diagrams_are_lifted(self) -> None:
        docs = prd_models([{"name": "prd/loja.md", "title": "PRD — Loja", "format": "markdown", "text": PRD}])
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertTrue(doc["text"].lstrip().startswith("erDiagram"))
        self.assertNotIn("```", doc["text"])
        self.assertEqual(doc["title"], "PRD — Loja — Modelo de dados")
        self.assertEqual(doc["source"], "prd/loja.md")
        self.assertIn("2 entidades", doc["summary"])

    def test_prd_without_der_gives_nothing(self) -> None:
        text = "# PRD\n\n```mermaid\nflowchart LR\n  a --> b\n```\n"
        self.assertEqual(prd_models([{"name": "p.md", "title": "PRD", "format": "markdown", "text": text}]), [])

    def test_several_diagrams_get_distinct_names(self) -> None:
        text = "# PRD\n\n```mermaid\nerDiagram\n  A ||--o{ B : x\n```\n\n```mermaid\nerDiagram\n  C ||--|| D : y\n```\n"
        docs = prd_models([{"name": "p.md", "title": "PRD", "format": "markdown", "text": text}])
        self.assertEqual([d["name"] for d in docs], ["p.md#der", "p.md#der-2"])
        self.assertEqual(len({d["title"] for d in docs}), 2)

    def test_html_prd(self) -> None:
        text = '<h1>PRD</h1><pre class="mermaid">erDiagram\n  A ||--o{ B : &quot;tem&quot;\n</pre>'
        docs = prd_models([{"name": "p.html", "title": "PRD", "format": "html", "text": text}])
        self.assertEqual(len(docs), 1)
        self.assertIn('"tem"', docs[0]["text"])

    def test_served_docs_carry_the_tab(self) -> None:
        from croqui.serve import _docs_with_modeling

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".croqui" / "prd").mkdir(parents=True)
            (root / ".croqui" / "prd" / "loja.md").write_text(PRD, encoding="utf-8")
            (root / ".croqui" / "graph.json").write_text(json.dumps({}), encoding="utf-8")
            self.assertEqual(len(collect_docs(root)["prd"]), 1)
            docs = _docs_with_modeling(root, root / ".croqui")
            self.assertEqual(len(docs["prd_model"]), 1)


if __name__ == "__main__":
    unittest.main()
