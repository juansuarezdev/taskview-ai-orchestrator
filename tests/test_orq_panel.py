"""Parsers of orq-panel.py: dispatcher log, web-tasks.txt, --usage and --dry-run output."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("orq_panel", Path(__file__).resolve().parent.parent / "orq-panel.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

LOG = """2026-11-23 12:01:43 start t442 exec [Project Board N] QA: XCUITest
2026-11-23 12:25:58 usage t442 exec claude-sonnet-5-5 cost=$2.25 5h=23% 7d=9%
2026-11-23 12:26:30 usage t442 review claude-sonnet-5-5 cost=$0.27 5h=24% 7d=9%
2026-11-23 12:30:00 t442 integrar main: main no está limpio; no se mergea
2026-11-23 13:06:30 end t442 ok=False ia·fallo runner=claude model=claude-sonnet-5-5 secs=3886
2026-11-23 13:10:00 t441 integrar main: tests (npm) rc=0 :: ok
2026-11-23 13:10:05 t441 integrar main: ai/t441 en main (53a87f6) y push a origin/main
2026-11-23 14:00:00 start t443 plan [Board V] Otra
garbage line
"""


class T(unittest.TestCase):
    def test_log(self):
        runs, integ = P.parse_log(LOG.splitlines())
        self.assertEqual([r["id"] for r in runs], [442, 443])
        r = runs[0]
        self.assertEqual((r["cost"], r["steps"], r["ok"], r["result"], r["secs"]), (2.52, 2, False, "ia·fallo", 3886))
        self.assertIsNone(runs[1]["end"])  # still open
        self.assertFalse(integ[442]["ok"])
        self.assertTrue(integ[441]["ok"])  # progress line (tests) does not override the final result

    def test_web(self):
        text = "441  # concedido\n# comentario\n"
        on = P.web_set(text, 500, True, "nota")
        self.assertEqual(P.web_ids(on), ["441", "500"])
        self.assertIn("# comentario", on)
        self.assertEqual(P.web_ids(P.web_set(on, 441, False)), ["500"])
        self.assertEqual(P.web_set(on, 500, True, "x").count("500"), 1)  # no duplicates

    def test_usage(self):
        u = P.parse_usage("5h window: 40% used, resets 16:50 (264 min into window)\n7d window: 10% (resets Sun 19:00)\n"
                          "pacing: allowed 100% now\nweek pacing: allowed 28% now\ndecision: OK to start Claude\n"
                          "claude: no cooldown · opencode: exhausted\n")
        self.assertEqual((u["h5"], u["h5_reset"], u["d7"], u["d7_reset"], u["h5_allowed"], u["d7_allowed"]), (40, "16:50", 10, "Sun 19:00", 100, 28))
        self.assertEqual((u["claude"], u["opencode"]), ("no cooldown", "exhausted"))

    def test_dry(self):
        rows = P.parse_dry("[Board N] t444 espera: la rama base ai/t443 aún no existe\n[Board V] t5 exec/claude base=main: Título\n[Home] sin columnas")
        self.assertEqual([(r["id"], r["kind"]) for r in rows], [(444, "espera"), (5, "lista"), (None, "aviso")])

    def test_question(self):
        tail = "contexto   PREGUNTA: ¿Uso SQLite o Postgres?   Sesión IA: abc mode=exec runner=claude"
        self.assertEqual(P.question_text(tail), "¿Uso SQLite o Postgres?")


if __name__ == "__main__":
    unittest.main()
