"""orq-config.json: defaults equal the old constants, validation, partial save, and how ai-dispatch.py applies it.
Run: python3 selfhost/tests/test_orq_config.py"""
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["ORQ_CONFIG_FILE"] = str(TMP / "orq-config.json")  # never the real file
sys.path.insert(0, str(HERE))
import orq_config as O  # noqa: E402


def dispatcher():
    spec = importlib.util.spec_from_file_location("ai_dispatch", HERE / "ai-dispatch.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    return d


class T(unittest.TestCase):
    def setUp(self):
        O.FILE.unlink(missing_ok=True)

    def test_defaults_are_the_old_constants(self):
        d = dispatcher()
        self.assertEqual(d.MERGE_TO_MAIN_REPOS, {"repo-b", "repo-v", "repo-n", "repo-s"})
        self.assertEqual(d.PRIORITY_PROJECTS, [32])
        self.assertEqual((d.PACE_HOURS, d.PACE_SLACK, d.PACE_HARD, d.WEEK_SLACK, d.WEEK_HARD), (4, 10, 90, 15, 90))
        self.assertEqual(d.USAGE_ALERTS, (70, 90))
        self.assertEqual(d.MAX_ROUNDS, 3)
        self.assertEqual(d.BUDGET, {"plan": "2", "exec": "5", "review": "2"})
        self.assertEqual(d.CFG_ERRORS, {})
        self.assertTrue(d.CFG["handoff_to_opencode"])
        self.assertIsNone(d.CFG["default_runner"])

    def test_validate(self):
        cfg, err = O.validate({"tick_secs": 30, "pace_hard": 95, "runner_both": "gpt", "nope": 1, "budget_usd": {"plan": 1, "exec": 3}, "usage_alerts": [90, 70, 70]})
        self.assertEqual(set(err), {"tick_secs", "runner_both", "nope", "budget_usd"})
        self.assertEqual((cfg["pace_hard"], cfg["usage_alerts"]), (95, [70, 90]))
        _, err = O.validate({"merge_to_main_repos": ["repo-n", "x"]}, {"merge_to_main_repos": ["repo-n"]})
        self.assertIn("x", err["merge_to_main_repos"])
        _, err = O.validate({"handoff_to_opencode": 1, "max_rounds": 2.5, "priority_tasks": [5, 3, 5]})
        self.assertEqual(set(err), {"handoff_to_opencode", "max_rounds"})
        self.assertEqual(O.validate({"priority_tasks": [5, 3, 5]})[0]["priority_tasks"], [5, 3])  # order kept, no duplicates

    def test_save_only_diffs_and_load_bad_file(self):
        cfg = {**O.DEFAULTS, "pace_hard": 80}
        self.assertEqual(O.save(cfg), {"pace_hard": 80})
        self.assertEqual(json.loads(O.FILE.read_text()), {"pace_hard": 80})
        self.assertEqual(O.load()[0]["pace_hard"], 80)
        O.FILE.write_text('{"pace_hard": 500, "week_hard": 70}')
        cfg, err = O.load()
        self.assertEqual((cfg["pace_hard"], cfg["week_hard"]), (90, 70))  # bad key -> default, good key kept
        self.assertIn("pace_hard", err)
        O.FILE.write_text("{roto")
        cfg, err = O.load()
        self.assertEqual(cfg, O.DEFAULTS)
        self.assertIn("_", err)

    def test_dispatcher_applies_config(self):
        O.FILE.write_text(json.dumps({"merge_to_main_repos": ["repo-n"], "pace_hard": 70, "budget_usd": {"plan": 1.5, "exec": 4, "review": 2},
                                      "runner_both": "opencode", "default_runner": "claude", "max_rounds": 1}))
        d = dispatcher()
        self.assertEqual((d.MERGE_TO_MAIN_REPOS, d.PACE_HARD, d.MAX_ROUNDS), ({"repo-n"}, 70, 1))
        self.assertEqual(d.BUDGET, {"plan": "1.5", "exec": "4", "review": "2"})
        owner, claude, oc = "owner@example.com", d.ia_users.RUNNERS["claude"], d.ia_users.RUNNERS["opencode"]
        self.assertEqual(d.select({"ia·ejecutar"}, {claude, oc}, owner), ("ia·ejecutar", "exec", "opencode"))
        self.assertEqual(d.select({"ia·plan"}, set(), owner), ("ia·plan", "plan", "claude"))  # nobody assigned -> default runner
        self.assertIsNone(d.select({"ia·plan"}, {"otra@persona.com"}, owner))  # someone else's card: still nothing
        self.assertIsNone(d.select({"ia·plan"}, {owner}, owner))  # the owner's card: never
        self.assertIsNone(d.select(set(), set(), owner))


if __name__ == "__main__":
    unittest.main()
