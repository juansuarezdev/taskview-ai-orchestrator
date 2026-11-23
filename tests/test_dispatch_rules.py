"""Permission rules and commit subject of ai-dispatch.py. Run: python3 selfhost/tests/test_dispatch_rules.py"""
import importlib.util, sys
from pathlib import Path
import os as _os, tempfile as _tf; _os.environ.setdefault("ORQ_CONFIG_FILE", str(Path(_tf.mkdtemp()) / "orq-config.json"))  # never the real file

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("ai_dispatch", HERE / "ai-dispatch.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

# The RTK hook rewrites commands before the permission check: each rule needs both forms.
for rule in ["Bash(git add:*)", "Bash(rtk git add:*)", "Bash(git commit:*)", "Bash(rtk git commit:*)", "Bash(rtk read:*)",
             "Bash(rtk php artisan test:*)", "Bash(rtk pint:*)", "Bash(rtk composer install:*)"]:
    assert rule in d.ALLOWED, rule
for rule in ["Bash(git push:*)", "Bash(rtk git push:*)", "Bash(rtk curl:*)", "Bash(rtk git commit --no-verify:*)"]:
    assert rule in d.DENIED, rule
assert not set(d.ALLOWED) & set(d.DENIED)

long = "Pre-deploy: auditar que ningún usuario vea datos de otra organización o sede (IDOR) en rutas y Filament"
for attempt in (1, 3):
    m = d.commit_message(368, long, attempt)
    assert len(m) <= 72 and m.startswith("chore(t368): ") and m.split("): ", 1)[1].split(" (ronda")[0].endswith("…"), m
assert d.commit_message(5, "Arreglo corto", 1) == "chore(t5): Arreglo corto"
assert d.commit_message(5, "Arreglo corto", 2) == "chore(t5): Arreglo corto (ronda 2)"

print("ok: rtk-aware rules and commit subjects")
