"""Selection rule of ai-dispatch.py: tags say what, assignees say who. Run: python3 tests/test_dispatch_select.py"""
import importlib.util, sys
from pathlib import Path
import os as _os2, tempfile as _tf2; _os2.environ.setdefault("ORQ_SITE_FILE", str(Path(_tf2.mkdtemp()) / "orq-site.json"))  # never the real site file
import os as _os, tempfile as _tf; _os.environ.setdefault("ORQ_CONFIG_FILE", str(Path(_tf.mkdtemp()) / "orq-config.json"))  # never the real file

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("ai_dispatch", HERE / "ai-dispatch.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

OWNER = "owner@example.com"
CLAUDE, OPENCODE = d.ia_users.RUNNERS["claude"], d.ia_users.RUNNERS["opencode"]
sel = lambda tags, who: d.select(set(tags), set(who), OWNER)

cases = [
    ((["ia·plan"], [CLAUDE]), ("ia·plan", "plan", "claude")),
    ((["ia·ejecutar"], [OPENCODE]), ("ia·ejecutar", "exec", "opencode")),
    ((["ia·ejecutar"], [CLAUDE, OPENCODE]), ("ia·ejecutar", "exec", "claude")),  # both: Claude
    ((["ia·ejecutar"], []), None),  # mode tag, no AI assignee
    ((["ia·plan"], ["someone@else.com"]), None),
    (([], [CLAUDE]), None),  # AI assignee, no mode tag
    ((["ia·ejecutar"], [CLAUDE, OWNER]), None),  # the user's own task is never touched
    ((["ia·pack·barato"], [OPENCODE]), ("ia·pack·barato", "exec", "opencode")),  # barato = ia·ejecutar + mail
    ((["ia·pack·analizar"], [CLAUDE]), ("ia·pack·analizar", "plan", "claude")),
    ((["ia·ejecutar", "ia·pack·silencioso"], [CLAUDE]), ("ia·pack·silencioso", "plan", "claude")),  # packs win
    ((["ia·opencode"], [OPENCODE]), None),  # retired trigger
    ((["para-mí", "ia·plan"], [CLAUDE]), ("ia·plan", "plan", "claude")),  # the old tag means nothing now
]
for (tags, who), want in cases:
    got = sel(tags, who)
    assert got == want, f"{tags} {who}: got {got}, want {want}"
assert "ia·opencode" not in d.TRIGGERS and "para-mí" not in d.USER_TAGS and "ia·opencode" not in d.TRIGGER_COLORS
assert d.PACKS["ia·pack·barato"]["trigger"] == "ia·ejecutar"
print(f"ok: {len(cases)} selection cases")
