"""Parsers of orq-panel.py: dispatcher log, web-tasks.txt, --usage and --dry-run output."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
import os as _os2, tempfile as _tf2; _os2.environ.setdefault("ORQ_SITE_FILE", str(Path(_tf2.mkdtemp()) / "orq-site.json"))  # never the real site file

TMP = Path(tempfile.mkdtemp())
os.environ["ORQ_CONFIG_FILE"] = str(TMP / "orq-config.json")  # never the real file
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
2026-11-23 14:00:00 start t443 plan [Board B] Otra
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
        rows = P.parse_dry("[Board A] t444 espera: la rama base ai/t443 aún no existe\n[Board B] t5 exec/claude base=main: Título\n[Home] sin columnas")
        self.assertEqual([(r["id"], r["kind"]) for r in rows], [(444, "espera"), (5, "lista"), (None, "aviso")])

    def test_question(self):
        tail = "contexto   PREGUNTA: ¿Uso SQLite o Postgres?   Sesión IA: abc mode=exec runner=claude"
        self.assertEqual(P.question_text(tail), "¿Uso SQLite o Postgres?")



class FakeAPI:
    """In-memory TaskView: task 10 on goal 5, collaborators 26 = Claude, 27 = OpenCode, 9 = the owner."""
    def __init__(self, tags=(), assigned=(26,), complete=False):
        self.tagdefs = [{"id": i, "name": n, "goalId": 5} for i, n in enumerate(["ia·plan", "ia·ejecutar", "ia·continuar", "para-mí", "ia·fallo", "ia·trabajando"], 100)]
        self.task = {"id": 10, "goalId": 5, "description": "Tarjeta", "tags": [t["id"] for t in self.tagdefs if t["name"] in tags],
                     "assignedUsers": list(assigned), "complete": complete}
        self.calls = []

    def __call__(self, m, path, body=None):
        if m == "GET" and path == "/module/tasks/10":
            return dict(self.task)
        if m == "GET" and path == "/module/tags":
            return self.tagdefs
        if m == "GET" and path == "/module/collaboration/5":
            return [{"id": 26, "email": "claude@taskview.local"}, {"id": 27, "email": "opencode@taskview.local"}, {"id": 9, "email": "owner@example.com"}]
        self.calls.append((m, path, body))
        if path == "/module/tags/toggle":
            ids = self.task["tags"]
            ids.remove(body["tagId"]) if body["tagId"] in ids else ids.append(body["tagId"])
        if path == "/module/tasks/task-users":
            self.task["assignedUsers"] = body["userIds"]
        return {}


class Ops(unittest.TestCase):
    def setUp(self):
        P.orq_config.FILE.unlink(missing_ok=True)
        P.WEB_TASKS = TMP / "web-tasks.txt"
        P.WEB_TASKS.write_text("")
        P.PANEL_LOG = TMP / "orq-panel.log"
        P.LOG = TMP / "ai-dispatch.log"
        P.LOG.write_text("2026-11-23 12:00:00 start t10 plan [Board] Tarjeta\n2026-11-23 12:10:00 end t10 ok=False ia·fallo runner=claude model=m secs=600\n")
        P.CACHE.update(owner="owner@example.com", goals=[{"id": 5, "name": "Board"}], goals_at=time.time())
        P.D = type("D", (), {"REPOS": {"Board": "board"}})()

    def use(self, fake):
        P._api_r = fake
        return fake

    def test_locked_cards_are_never_touched(self):
        for kw, why in ((dict(tags=["para-mí"]), "para-mí"), (dict(assigned=[9, 26]), "dueño"), (dict(complete=True), "completada")):
            f = self.use(FakeAPI(**kw))
            for op, b in (("tag", {"name": "ia·plan", "on": True}), ("assign", {"runner": "opencode", "on": True}), ("web", {"on": True}),
                          ("relaunch", {}), ("priority", {"move": "top"})):
                with self.assertRaises(ValueError) as e:
                    P.task_op(10, op, b)
                self.assertIn(why, str(e.exception))
            self.assertEqual(f.calls, [])
            with self.assertRaises(ValueError):
                P.web(10, True)
            self.assertEqual(P.WEB_TASKS.read_text(), "")
        self.assertEqual(P.lock_reason(set(), set(), "", False), "falta el email del dueño en ia-users.json")

    def test_assign_and_tags(self):
        f = self.use(FakeAPI(tags=["ia·fallo"]))
        c = P.task_op(10, "assign", {"runner": "opencode", "on": True})
        self.assertEqual(c["runners"], ["claude", "opencode"])
        c = P.task_op(10, "assign", {"runner": "claude", "on": False})
        self.assertEqual((c["runners"], f.task["assignedUsers"]), (["opencode"], [27]))
        c = P.task_op(10, "tag", {"name": "ia·ejecutar", "on": True})
        self.assertIn("ia·ejecutar", c["tags"])
        P.task_op(10, "tag", {"name": "ia·ejecutar", "on": True})  # already on: no second toggle
        self.assertEqual(len([x for x in f.calls if x[1] == "/module/tags/toggle"]), 1)
        with self.assertRaises(ValueError):
            P.task_op(10, "tag", {"name": "para-mí", "on": True})
        with self.assertRaises(ValueError):
            P.task_op(10, "relaunch", {})  # already queued
        self.assertIn("t10 asignada a opencode", P.PANEL_LOG.read_text())

    def test_relaunch_uses_last_mode(self):
        f = self.use(FakeAPI(tags=["ia·fallo"]))
        c = P.task_op(10, "relaunch", {})
        self.assertIn("ia·plan", c["tags"])  # the log says the last run was a plan
        f = self.use(FakeAPI(tags=["ia·fallo"], assigned=[]))
        with self.assertRaises(ValueError):
            P.task_op(10, "relaunch", {})  # no AI assignee: it would not run

    def test_priority_and_web(self):
        self.use(FakeAPI())
        P.orq_config.save({**P.orq_config.DEFAULTS, "priority_tasks": [7, 8]})
        self.assertEqual(P.task_op(10, "priority", {"move": "top"})["priority"], 1)
        self.assertEqual(P.task_op(10, "priority", {"move": "down"})["priority"], 2)
        self.assertEqual(P.orq_config.load()[0]["priority_tasks"], [7, 10, 8])
        self.assertIsNone(P.task_op(10, "priority", {"move": "remove"})["priority"])
        self.assertTrue(P.task_op(10, "web", {"on": True})["web"])
        self.assertEqual(P.web_ids(P.WEB_TASKS.read_text()), ["10"])

    def test_save_config_validates_and_logs(self):
        with self.assertRaises(ValueError):
            P.save_config({**P.orq_config.DEFAULTS, "merge_to_main_repos": ["otro"]})
        with self.assertRaises(ValueError):
            P.save_config({**P.orq_config.DEFAULTS, "tick_secs": 10})
        self.assertFalse(P.orq_config.FILE.exists())
        P.save_config({**P.orq_config.DEFAULTS, "merge_to_main_repos": ["board"], "pace_hard": 80})
        self.assertEqual(json.loads(P.orq_config.FILE.read_text()), {"merge_to_main_repos": ["board"], "pace_hard": 80})
        self.assertIn('pace_hard 90 -> 80', P.PANEL_LOG.read_text())

    def test_latest(self):
        P.LOG.write_text(P.LOG.read_text().replace("2026-11-23", "2026-11-01"))
        runs, _ = P.parse_log(P.LOG.read_text().splitlines())
        L = P.latest(runs, {"questions": [{"id": 3, "description": "a", "noteTail": "PREGUNTA: ¿x?"}, {"id": 10, "description": "b", "noteTail": "PREGUNTA: ¿y?"}]})
        self.assertEqual((L["question"]["id"], L["question"]["text"]), (10, "¿y?"))  # the one that stopped last
        self.assertEqual(L["relaunch"], None)  # the failure is older than 3 days (fixed date in the log)
        P.LOG.write_text(P.LOG.read_text().replace("2026-11-01 12:10:00", time.strftime("%Y-%m-%d %H:%M:%S")))
        runs, _ = P.parse_log(P.LOG.read_text().splitlines())
        self.assertEqual(P.latest(runs, {})["relaunch"]["id"], 10)

    def test_phonebox_answer_waits_for_a_fresh_question(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        P.LOG.write_text(f"{now} start t10 exec [Board] Tarjeta\n{now} end t10 ok=False ia·pregunta runner=claude model=m secs=1\n")
        P._api_r = lambda m, p, b=None: {"questions": [{"id": 10, "description": "b", "noteTail": "PREGUNTA: ¿y?"}]}
        with self.assertRaises(ValueError) as e:
            P.answer_latest("", "PhoneBox")
        self.assertIn("acaba de preguntar", str(e.exception))


if __name__ == "__main__":
    unittest.main()
