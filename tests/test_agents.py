"""The /croqui skill: renders for every tool, stays valid, and points at the cheap path."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from croqui.agents import BODY, TARGETS, _gemini_command, install


class SkillTests(unittest.TestCase):
    def test_renders_for_all_three_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            written = install(list(TARGETS), base)
            tools = {tool for tool, _ in written}
            self.assertEqual(tools, {"claude", "codex", "gemini"})
            for _, path in written:
                text = path.read_text(encoding="utf-8")
                self.assertIn("croqui draw", text, path.name)
                self.assertIn("croqui lint", text, path.name)
                self.assertNotIn("{request}", text)

    def test_gemini_toml_is_valid(self) -> None:
        data = tomllib.loads(_gemini_command())
        self.assertIn("{{args}}", data["prompt"])
        self.assertIn("croqui draw", data["prompt"])
        self.assertNotIn("'''", BODY)

    def test_cheapest_path_comes_first(self) -> None:
        body = BODY.format(request="x")
        self.assertLess(body.index("croqui draw"), body.index("Writing one by hand"))
        self.assertLess(body.index("croqui lint"), body.index("croqui build ."))
        self.assertIn("%% croqui:gerado", body)

    def test_stays_short(self) -> None:
        # The skill is read by the model on every use; the previous version was 544 words.
        self.assertLess(len(BODY.split()), 500)


if __name__ == "__main__":
    unittest.main()
