#!/usr/bin/env python3
"""Adds the small orchestrator card to PhoneBox (idempotent): a Custom HTML widget on the "Sessions" page
(GitHub widget shrinks to the top half) and two shortcuts, orq-pausar / orq-reanudar, that the card runs.
Backs up config.json first. Undo: copy the backup back, or run with --remove."""
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = Path.home() / "Library/Application Support/PhoneBox/config.json"
PAGE, WID = "work", "orquestador"
PANEL = str(HERE / "orq-panel.py")
SHORTCUTS = [
    {"id": "orq-pausar", "label": "Pausar orquestador", "icon": "⏸️", "color": "#e9a84a", "cmd": "/usr/bin/python3",
     "args": f"{PANEL} --pause --phonebox", "toggle": False},
    {"id": "orq-reanudar", "label": "Reanudar orquestador", "icon": "▶️", "color": "#16834a", "cmd": "/usr/bin/python3",
     "args": f"{PANEL} --resume --phonebox", "toggle": False},
]


def main():
    remove = "--remove" in sys.argv
    c = json.loads(CFG.read_text())
    backup = CFG.with_name(f"config.json.bak-{time.strftime('%F')}-orq")
    if not backup.exists():
        shutil.copy2(CFG, backup)
    page = next(p for p in c["pages"] if p["id"] == PAGE)
    ws = [w for w in page["widgets"] if w["id"] != WID]
    gh = next((w for w in ws if w["type"] == "github"), None)
    if remove:
        if gh:
            gh.update(y=0, h=6)
    else:
        if gh:
            gh.update(y=0, h=3)
        ws.append({"id": WID, "type": "html", "x": 7, "y": 3, "w": 5, "h": 3,
                   "options": {"html": (HERE / "phonebox-widget.html").read_text()}})
    page["widgets"] = ws
    ids = {s["id"] for s in SHORTCUTS}
    c["shortcuts"] = [s for s in c.get("shortcuts", []) if s["id"] not in ids] + ([] if remove else SHORTCUTS)
    CFG.write_text(json.dumps(c, ensure_ascii=False, indent=2))
    CFG.chmod(0o600)
    print(("quitado" if remove else "instalado") + f"; copia en {backup}")


if __name__ == "__main__":
    main()
