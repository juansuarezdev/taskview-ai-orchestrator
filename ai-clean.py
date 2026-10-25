#!/usr/bin/env python3
"""List the git worktrees created by ai-dispatch (~/Projects/code/.ai-worktrees) and whether their task is Done.

Default is a dry run. `--remove` deletes only worktrees whose task is Done and whose branch has no unmerged
commits; the branch itself is kept. If `git log main..ai/t<id>` fails (missing repo, branch or `main`) the
worktree is 'not verifiable': it is never removed and the reason is shown in the text line.

`--json` prints a JSON list of {repo, task_id, state ('Done'/'abierta'), unmerged (number of unmerged commits,
or null when git could not check the branch)}
instead of text lines (`[]` when there are no worktrees). Combined with `--remove`, the JSON shows the state
before removing. Unknown arguments exit with code 2 and a usage message.
"""
import argparse, json, re, subprocess, urllib.request
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
    ap = argparse.ArgumentParser(description="List ai-dispatch worktrees and whether their task is Done.")
    ap.add_argument("--remove", action="store_true", help="remove worktrees whose task is Done and have no unmerged commits")
    ap.add_argument("--json", action="store_true", help="print the worktree list as JSON")
    args = ap.parse_args()
    if not WT.exists():
        return print("[]" if args.json else "sin worktrees")
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    done = set()
    for g in api("GET", "/module/goals"):
        for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": g["id"]}):
            if s["name"] == "Done":
                done |= {t["id"] for t in api("GET", f"/module/kanban/tasks/{g['id']}/{s['id']}/null")["tasks"]}
    rows = []
    for wt in sorted(WT.iterdir()):
        m = re.match(r"(.+)-t(\d+)$", wt.name)
        if not m:
            continue
        repo, tid = Path.home() / "Projects/code" / m[1], int(m[2])
        r = subprocess.run(["git", "-C", str(repo), "log", "--oneline", f"main..ai/t{tid}"], capture_output=True, text=True)
        state = "Done" if tid in done else "abierta"
        verifiable = r.returncode == 0
        n = len(r.stdout.strip().splitlines()) if verifiable else None
        rows.append({"repo": m[1], "task_id": tid, "state": state, "unmerged": n})
        if not args.json:
            if verifiable:
                print(f"{wt.name}: tarea {state}, {n} commits sin mergear")
            else:
                print(f"{wt.name}: tarea {state}, no verificable ({r.stderr.strip() or 'git log falló'}); no se elimina")
        if args.remove and tid in done and verifiable and n == 0:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(wt)])
            if not args.json:
                print("  eliminado")
    if args.json:
        print(json.dumps(rows))


if __name__ == "__main__":
    main()
