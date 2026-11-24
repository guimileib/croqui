"""croqui plan / croqui next: a request becomes steps, and steps are handed over."""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from croqui import llm
from croqui.plan import draft, next_step, plans, steps

ANSWER = """# Plano — exportar pedidos

## Pedido
Exportar os pedidos em CSV.

## Contexto
Toca `GET /orders`.

## Etapas

### [ ] 1. Criar o endpoint
**O que fazer:** `GET /orders/export`.
**Como verificar:** `pytest tests/test_export.py`

### [ ] 2. Botão na tela
**O que fazer:** link para o endpoint.

## Fora de escopo
Excel.
"""


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.said: list[str] = []
        self.when = datetime.datetime(2026, 11, 24, 10, 0, 0)

    def plan(self, request: str, answer: str = ANSWER, minutes: int = 0, **kw) -> int:
        engine = llm.Engine(name="fake", kind="cli", how="um modelo de teste")
        with mock.patch.object(llm, "resolve", return_value=engine), \
                mock.patch.object(llm.Engine, "complete", return_value=answer):
            return draft(self.root, request, say=self.said.append,
                         now=self.when + datetime.timedelta(minutes=minutes), **kw)

    def next(self, **kw) -> str:
        self.said.clear()
        self.code = next_step(self.root, say=self.said.append, today=self.when.date(), **kw)
        return "\n".join(self.said)

    def test_a_request_becomes_a_file_of_steps(self) -> None:
        self.assertEqual(self.plan("Exportar pedidos em CSV"), 0)
        (path,) = plans(self.root)
        self.assertEqual(path.name, "pedido-20261124-100000-exportar-pedidos-em-csv.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("croqui:plano", text.splitlines()[0])
        self.assertEqual([(s["n"], s["title"], s["done"]) for s in steps(text)],
                         [(1, "Criar o endpoint", False), (2, "Botão na tela", False)])

    def test_an_answer_without_steps_writes_nothing(self) -> None:
        self.assertEqual(self.plan("x", answer="# Plano\n\nfaça tudo de uma vez"), 1)
        self.assertEqual(plans(self.root), [])

    def test_manual_needs_no_model(self) -> None:
        self.assertEqual(draft(self.root, "Trocar o logo", manual=True, say=self.said.append, now=self.when), 0)
        self.assertEqual(len(steps(plans(self.root)[0].read_text(encoding="utf-8"))), 2)

    def test_the_next_agent_gets_the_step_and_the_notes(self) -> None:
        self.plan("Exportar pedidos em CSV")
        out = self.next()
        self.assertIn("etapa 1 de 2", out)
        self.assertIn("pedido: Exportar os pedidos em CSV.", out)
        self.assertIn("### [ ] 1. Criar o endpoint", out)
        self.assertNotIn("Botão na tela", out)

        out = self.next(done=1, note="rota em orders.py;\n  falta paginar")
        self.assertIn("[x] 1. Criar o endpoint  feito em 2026-11-24: rota em orders.py; falta paginar", out)
        self.assertIn("### [ ] 2. Botão na tela", out)
        text = plans(self.root)[0].read_text(encoding="utf-8")
        self.assertIn("### [x] 1. Criar o endpoint", text)
        self.assertIn("## Fora de escopo\nExcel.", text)

        self.assertIn("não tem etapa 1 em aberto", self.next(done=1))
        self.assertEqual(self.code, 1)
        self.assertIn("plano concluído", self.next(done=2))
        self.assertIn("todas as etapas", self.next())

    def test_requests_are_worked_oldest_first(self) -> None:
        self.plan("Zebra primeiro")
        self.plan("Abacaxi depois", minutes=5)
        self.assertIn("zebra-primeiro", self.next())
        self.next(done=1)
        self.next(done=2)
        self.assertIn("abacaxi-depois", self.next())
        self.assertIn("abacaxi-depois", self.next(plan="abacaxi"))

    def test_nothing_to_do(self) -> None:
        self.assertIn("nenhum plano", self.next())
        self.assertEqual(self.code, 1)
        self.assertEqual(draft(self.root, "  ", say=self.said.append), 2)


if __name__ == "__main__":
    unittest.main()
