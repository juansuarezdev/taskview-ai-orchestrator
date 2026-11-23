"""Global settings of ai-dispatch.py that the orchestrator panel (orq-panel.py) can edit.

The values live in selfhost/orq-config.json (gitignored, local state). A missing file, a missing key or an invalid value
falls back to DEFAULTS, which are the constants ai-dispatch.py had hard-coded, so deleting the file restores the old
behaviour. ai-dispatch.py reads it on every tick (each launchd run is a new process); the panel validates before saving.
"""
import copy
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = Path(os.environ.get("ORQ_CONFIG_FILE") or HERE / "orq-config.json")
RUNNERS = ("claude", "opencode")

DEFAULTS = {
    "tick_secs": 60,                      # launchd starts the dispatcher every 60 s; larger values skip ticks
    "runner_both": "claude",              # who runs a task assigned to both AIs
    "default_runner": None,               # mode tag and nobody assigned: None = nothing runs, or "claude" / "opencode"
    "handoff_to_opencode": True,          # Claude paced, reserved or in cooldown -> exec cards continue with OpenCode
    "pace_hours": 4,                      # the 5 h window must last at least this long
    "pace_slack": 10,                     # % on top of the straight line (5 h)
    "pace_hard": 90,                      # % of the 5 h window from which no Claude run starts
    "week_slack": 15,                     # % on top of the straight line (week)
    "week_hard": 90,                      # % of the week from which no Claude run starts
    "usage_alerts": [70, 90],             # one phone alert per window at each level
    "max_rounds": 3,                      # execute + review rounds
    "budget_usd": {"plan": 2, "exec": 5, "review": 2},
    "merge_to_main_repos": ["repo-b", "repo-n", "repo-s", "repo-v"],
    "disabled_boards": [],                # board names the dispatcher skips (they stay in REPOS)
    "priority_projects": [32],            # goal ids served first on every tick (32 = Board S)
    "priority_tasks": [],                 # task ids tried first: their board goes first and they lead their column
}

# key -> (kind, extra); kind drives validation and the panel form
SPEC = {
    "tick_secs": ("int", (60, 3600)),
    "runner_both": ("choice", RUNNERS),
    "default_runner": ("choice", (None, *RUNNERS)),
    "handoff_to_opencode": ("bool", None),
    "pace_hours": ("int", (1, 5)),
    "pace_slack": ("int", (0, 100)),
    "pace_hard": ("int", (1, 100)),
    "week_slack": ("int", (0, 100)),
    "week_hard": ("int", (1, 100)),
    "usage_alerts": ("ints", (1, 100)),
    "max_rounds": ("int", (1, 6)),
    "budget_usd": ("budget", (0.1, 50)),
    "merge_to_main_repos": ("strs", None),
    "disabled_boards": ("strs", None),
    "priority_projects": ("ints", (1, 10 ** 9)),
    "priority_tasks": ("ints", (1, 10 ** 9)),
}


def _num(v, lo, hi, cast):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (cast is int and float(v) != int(v)):
        raise ValueError("no es un número" + (" entero" if cast is int else ""))
    v = cast(v)
    if not lo <= v <= hi:
        raise ValueError(f"fuera de rango ({lo}–{hi})")
    return v


def check(key, v, choices=None):
    """Clean value for one key or ValueError with the reason. `choices` limits strs keys (e.g. known repos/boards)."""
    kind, extra = SPEC[key]
    if kind == "int":
        return _num(v, *extra, int)
    if kind == "bool":
        if not isinstance(v, bool):
            raise ValueError("debe ser sí o no")
        return v
    if kind == "choice":
        if v not in extra:
            raise ValueError(f"valor no válido ({', '.join(str(x) for x in extra)})")
        return v
    if kind == "ints":
        if not isinstance(v, list) or len(v) > 50:
            raise ValueError("debe ser una lista")
        return sorted({_num(x, *extra, int) for x in v}) if key != "priority_tasks" else list(dict.fromkeys(_num(x, *extra, int) for x in v))
    if kind == "strs":
        if not isinstance(v, list) or len(v) > 50 or not all(isinstance(x, str) and x.strip() for x in v):
            raise ValueError("debe ser una lista de nombres")
        out = sorted({x.strip() for x in v})
        bad = [x for x in out if choices is not None and x not in choices]
        if bad:
            raise ValueError(f"desconocido: {', '.join(bad)}")
        return out
    if kind == "budget":
        if not isinstance(v, dict) or set(v) != {"plan", "exec", "review"}:
            raise ValueError("faltan plan, exec o review")
        return {k: round(_num(v[k], *extra, float), 2) for k in ("plan", "exec", "review")}
    raise ValueError("clave desconocida")


def validate(data, choices=None):
    """(clean full config, {key: error}). Unknown keys are errors; missing keys take the default.
    choices: {key: allowed names} for strs keys."""
    errors, out = {}, copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        return out, {"_": "no es un objeto JSON"}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if k not in SPEC:
            errors[k] = "clave desconocida"
            continue
        try:
            out[k] = check(k, v, (choices or {}).get(k))
        except ValueError as e:
            errors[k] = str(e)
    return out, errors


def load(path=None):
    """(config, errors). Never raises: an unreadable or invalid file gives the defaults for the bad keys."""
    p = Path(path or FILE)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS), {}
    except (OSError, ValueError) as e:
        return copy.deepcopy(DEFAULTS), {"_": f"no se pudo leer {p.name}: {e}"}
    cfg, errors = validate(raw)
    if errors:  # keep the valid keys, defaults for the rest
        good = {k: v for k, v in raw.items() if k in SPEC and k not in errors} if isinstance(raw, dict) else {}
        cfg, _ = validate(good)
    return cfg, errors


def save(cfg, path=None):
    """Write only the keys that differ from DEFAULTS (atomic rename), so new defaults reach untouched keys."""
    p = Path(path or FILE)
    diff = {k: v for k, v in cfg.items() if k in SPEC and v != DEFAULTS[k]}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(diff, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, p)
    return diff
