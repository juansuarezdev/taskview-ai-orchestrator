#!/usr/bin/env python3
"""List the git worktrees created by ai-dispatch (~/Projects/code/.ai-worktrees) and whether their task is Done.

Default is a dry run. `--remove` deletes only worktrees whose task is Done and whose branch has no unmerged
commits. It also deletes `ai/t<id>` branches (local, and on origin if pushed) once their PR was merged on GitHub,
or their task is Done and the branch is already merged into main. If `git log main..ai/t<id>` fails (missing repo, branch or `main`) the
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


REPO_NAMES = ["repo-m", "repo-g", "repo-e", "taskview-community"]


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def pr_merged(repo, branch):
    r = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
                       cwd=repo, capture_output=True, text=True)
    try:
        return bool(json.loads(r.stdout))
    except ValueError:
        return False


def prune_branches(done, apply, say):
    """Delete ai/t<id> branches whose PR was accepted (or whose task is Done and already merged into main)."""
    for name in REPO_NAMES:
        repo = Path.home() / "Projects/code" / name
        if not (repo / ".git").exists():
            continue
        for br in git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/ai/").stdout.split():
            m = re.fullmatch(r"ai/t(\d+)", br)
            if not m:
                continue
            by_pr = pr_merged(repo, br)
            if not (by_pr or (int(m[1]) in done and git(repo, "merge-base", "--is-ancestor", br, "main").returncode == 0)):
                continue
            why = "PR aceptada" if by_pr else "tarea Done y rama ya en main"
            if not apply:
                say(f"{name}/{br}: {why}; se borraría")
                continue
            wt = WT / f"{name}-t{m[1]}"
            if wt.exists() and git(repo, "worktree", "remove", str(wt)).returncode != 0:
                say(f"{name}/{br}: {why}, pero su worktree tiene cambios; no se borra")
                continue
            if git(repo, "branch", "-D" if by_pr else "-d", br).returncode != 0:
                say(f"{name}/{br}: no se pudo borrar la rama local")
                continue
            msg = f"{name}/{br}: {why}; rama local borrada"
            if git(repo, "ls-remote", "--exit-code", "--heads", "origin", br).returncode == 0:
                msg += ", y en origin" if git(repo, "push", "origin", "--delete", br).returncode == 0 else ", pero falló el borrado en origin"
            say(msg)


def main():
    ap = argparse.ArgumentParser(description="List ai-dispatch worktrees and whether their task is Done.")
    ap.add_argument("--remove", action="store_true", help="remove worktrees whose task is Done and have no unmerged commits")
    ap.add_argument("--json", action="store_true", help="print the worktree list as JSON")
    args = ap.parse_args()
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    done = set()
    for g in api("GET", "/module/goals"):
        for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": g["id"]}):
            if s["name"] == "Done":
                done |= {t["id"] for t in api("GET", f"/module/kanban/tasks/{g['id']}/{s['id']}/null")["tasks"]}
    rows = []
    for wt in sorted(WT.iterdir()) if WT.exists() else []:
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
                err = next((l.strip() for l in r.stderr.splitlines() if l.strip()), "git log falló")
                print(f"{wt.name}: tarea {state}, no verificable ({err}); no se elimina")
        if args.remove and tid in done and verifiable and n == 0:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(wt)])
            if not args.json:
                print("  eliminado")
    prune_branches(done, args.remove, (lambda _: None) if args.json else print)
    if args.json:
        print(json.dumps(rows))


if __name__ == "__main__":
    main()
