#!/usr/bin/env python3
"""Run TaskView tasks tagged for an AI agent.

Tag a task (not `para-mí`, status Backlog/TODO) in a project board with:
  ia·plan      -> claude plans only (read-only), writes the plan into the task note, moves it to Review
  ia·ejecutar  -> claude implements it in its own git worktree/branch `ai/t<id>`, never pushes, moves it to Review
Progress tags: ia·trabajando while running, then ia·plan-listo / ia·hecho / ia·fallo.
One task at a time (flock). Run every minute from launchd. `--dry-run` only lists.
"""
import fcntl, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"
CODE = Path.home() / "Projects/code"
WORKTREES = CODE / ".ai-worktrees"
LOG = HERE / "logs/ai-dispatch.log"
REPOS = {"Board M": "repo-m", "Board G": "repo-g", "Board E": "repo-e", "TaskView": "taskview-community"}
TRIGGERS = {"ia·plan": "plan", "ia·ejecutar": "exec"}
PROGRESS = {"ia·trabajando": "#f59e0b", "ia·plan-listo": "#3b82f6", "ia·hecho": "#22c55e", "ia·fallo": "#ef4444"}
TRIGGER_COLORS = {"ia·plan": "#3b82f6", "ia·ejecutar": "#f97316"}
BUDGET = {"plan": "1.5", "exec": "5"}
TIMEOUT = {"plan": 15 * 60, "exec": 40 * 60}
ALLOWED = ["Read", "Glob", "Grep", "Edit", "Write", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)",
           "Bash(git commit:*)", "Bash(ls:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(pnpm:*)", "Bash(python3:*)", "Bash(node:*)"]
DENIED = ["Bash(git push:*)", "Bash(rm:*)", "Bash(curl:*)", "Bash(sudo:*)", "Bash(git reset:*)", "Bash(git checkout main:*)"]


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json"}
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


def main():
    dry = "--dry-run" in sys.argv
    lock = open(HERE / "logs/.ai-dispatch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]

    for g in api("GET", "/module/goals"):
        repo = REPOS.get(g["name"])
        if not repo:
            continue
        gid = g["id"]
        # the endpoint ignores ?goalId and returns every goal's tags, and toggling needs the tag of the task's own goal
        tags = {t["name"]: t["id"] for t in api("GET", f"/module/tags?goalId={gid}") if t["goalId"] == gid}
        for name, color in {**TRIGGER_COLORS, **PROGRESS}.items():  # make the tags exist so they can be picked in the UI
            if name not in tags and not dry:
                tags[name] = api("POST", "/module/tags", {"name": name, "color": color, "goalId": gid})["id"]
        if not any(n in tags for n in TRIGGERS):
            continue
        st = {s["name"]: s["id"] for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": gid})}
        for sname in ("TODO", "Backlog"):
            if sname not in st:
                continue
            cur = "null"
            while cur:
                r = api("GET", f"/module/kanban/tasks/{gid}/{st[sname]}/{cur}")
                for t in r["tasks"]:
                    have = set(t.get("tags") or [])
                    if tags.get("para-mí") in have:
                        continue
                    mode = next((m for n, m in TRIGGERS.items() if tags.get(n) in have), None)
                    if mode:
                        if dry:
                            print(f"[{g['name']}] t{t['id']} {mode}: {t['description']}")
                        else:
                            run(api, g, repo, st, tags, t, mode)
                            return  # one task per tick
                cur = str(r["nextCursor"]) if r.get("nextCursor") else ""


def toggle(api, tags, t, name, on):
    has = tags[name] in set(t.get("tags") or [])
    if has != on:
        api("PATCH", "/module/tags/toggle", {"tagId": tags[name], "taskId": t["id"]})
        t["tags"] = (list(t.get("tags") or []) + [tags[name]]) if on else [x for x in t["tags"] if x != tags[name]]


def run(api, goal, repo, st, tags, t, mode):
    tid, trig = t["id"], "ia·plan" if mode == "plan" else "ia·ejecutar"
    log(f"start t{tid} {mode} [{goal['name']}] {t['description']}")
    toggle(api, tags, t, trig, False)
    toggle(api, tags, t, "ia·trabajando", True)
    api("PATCH", "/module/tasks", {"id": tid, "statusId": st["In Progress"]})
    note = t.get("note") or ""
    base = CODE / repo
    branch, wt = f"ai/t{tid}", WORKTREES / f"{repo}-t{tid}"
    ok, out = False, ""
    try:
        if not wt.exists():
            WORKTREES.mkdir(exist_ok=True)
            start = "main" if subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", "main"], capture_output=True).returncode == 0 else "HEAD"
            subprocess.run(["git", "-C", str(base), "worktree", "add", "-b", branch, str(wt), start], check=True, capture_output=True)
        if mode == "plan":
            prompt = (f"Tarea de TaskView #{tid}: {t['description']}\n\nNotas:\n{note}\n\n"
                      "Analiza el repo y escribe un plan de implementación concreto: archivos a tocar, pasos, riesgos y cómo verificarlo. "
                      "NO modifiques nada. Responde en español, conciso, en markdown.")
            perm = "plan"
        else:
            prompt = (f"Implementa la tarea de TaskView #{tid}: {t['description']}\n\nNotas (puede incluir un plan aprobado):\n{note}\n\n"
                      f"Trabajas en la rama {branch} de un git worktree. Haz commits pequeños con mensaje claro, sin firmas de IA. "
                      "No hagas push, no toques .env ni secretos, no borres nada fuera de lo necesario. Verifica con los tests/build del repo si existen. "
                      "Termina con un resumen en español: qué cambió, cómo verificarlo y qué queda pendiente.")
            perm = "acceptEdits"
        env = {**os.environ, "TASKVIEW_NOHOOK": "1"}
        p = subprocess.run(["claude", "-p", prompt, "--permission-mode", perm, "--max-budget-usd", BUDGET[mode],
                            "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED],
                           cwd=wt, env=env, capture_output=True, text=True, timeout=TIMEOUT[mode])
        out, ok = (p.stdout or p.stderr or "").strip(), p.returncode == 0
    except subprocess.TimeoutExpired:
        out = f"Timeout ({TIMEOUT[mode] // 60} min)."
    except Exception as e:
        out = f"Error: {e}"
    stamp = time.strftime("%F %H:%M")
    head = {"plan": "Plan de la IA", "exec": "Resultado de la IA"}[mode]
    extra = f"\nRama: {branch} · worktree: {wt}" if mode == "exec" else ""
    new_note = f"{note}\n\n--- {head} ({stamp}){' — FALLÓ' if not ok else ''} ---\n{out[-6000:]}{extra}".strip()
    done_tag = "ia·fallo" if not ok else ("ia·plan-listo" if mode == "plan" else "ia·hecho")
    toggle(api, tags, t, "ia·trabajando", False)
    toggle(api, tags, t, done_tag, True)
    api("PATCH", "/module/tasks", {"id": tid, "note": new_note, "statusId": st["Review"]})
    log(f"end t{tid} ok={ok}")
    subprocess.run([sys.executable, str(HERE / "notify.py"), f"IA {'ok' if ok else 'fallo'}: {t['description'][:60]}",
                    {"plan": "Plan listo para revisar", "exec": "Implementación lista en Review"}[mode] if ok else "Revisa la nota de la tarea", "3" if ok else "4"])


if __name__ == "__main__":
    main()
