"""Per-task plan of migrate-assignees.py. Run: python3 selfhost/tests/test_migrate_plan.py"""
import importlib.util, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("migrate", HERE / "migrate-assignees.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

OWNER = "owner@example.com"
C, O = m.ia_users.RUNNERS["claude"], m.ia_users.RUNNERS["opencode"]
plan = lambda tags, who=(): m.plan_task(set(tags), set(who), OWNER)

cases = [
    ((["para-mí"],), ({OWNER}, set(), {"para-mí"})),
    ((["para-mí"], [OWNER]), (set(), set(), {"para-mí"})),  # already assigned: only the tag goes
    ((["ia·opencode"],), ({O}, {"ia·ejecutar"}, {"ia·opencode"})),
    ((["ia·opencode", "ia·ejecutar"], [O]), (set(), set(), {"ia·opencode"})),
    ((["ia·plan"],), ({C}, set(), set())),
    ((["ia·pack·barato"],), ({C}, set(), set())),
    ((["ia·plan"], [O]), (set(), set(), set())),  # an AI is already assigned: keep it
    ((["ia·plan", "para-mí"],), ({OWNER}, set(), {"para-mí"})),  # the user's task: never given to Claude
    ((["para-mí", "ia·opencode"],), ({OWNER}, set(), {"para-mí", "ia·opencode"})),
    ((["ia·hecho"],), (set(), set(), set())),
    (([],), (set(), set(), set())),
]
for args, want in cases:
    got = plan(*args)
    assert got == want, f"{args}: got {got}, want {want}"
# idempotent: applying the plan and planning again gives nothing
for args, _ in cases:
    tags, who = set(args[0]), set(args[1]) if len(args) > 1 else set()
    add_who, add_tags, rm_tags = m.plan_task(tags, who, OWNER)
    assert m.plan_task((tags | add_tags) - rm_tags, who | add_who, OWNER) == (set(), set(), set()), args
print(f"ok: {len(cases)} migration cases, idempotent")
