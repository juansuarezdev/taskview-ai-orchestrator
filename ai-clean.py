#!/usr/bin/env python3
"""List the git worktrees created by ai-dispatch (~/Projects/code/.ai-worktrees) and whether their task is Done.

Default is a dry run. `--remove` deletes only worktrees whose task is Done and whose branch has no unmerged
commits; the branch itself is kept.
"""
import json, re, subprocess, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WT = Path.home() / "Projects/code/.ai-worktrees"
API = "http://127.0.0.1:1725"


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json"}
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


def main():
    if not WT.exists():
        return print("sin worktrees")
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    done = set()
    for g in api("GET", "/module/goals"):
        for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": g["id"]}):
            if s["name"] == "Done":
                done |= {t["id"] for t in api("GET", f"/module/kanban/tasks/{g['id']}/{s['id']}/null")["tasks"]}
    for wt in sorted(WT.iterdir()):
        m = re.match(r"(.+)-t(\d+)$", wt.name)
        if not m:
            continue
        repo, tid = Path.home() / "Projects/code" / m[1], int(m[2])
        unmerged = subprocess.run(["git", "-C", str(repo), "log", "--oneline", f"main..ai/t{tid}"], capture_output=True, text=True).stdout.strip()
        state = "Done" if tid in done else "abierta"
        print(f"{wt.name}: tarea {state}, {len(unmerged.splitlines())} commits sin mergear")
        if "--remove" in sys.argv and tid in done and not unmerged:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(wt)])
            print("  eliminado")


if __name__ == "__main__":
    main()
