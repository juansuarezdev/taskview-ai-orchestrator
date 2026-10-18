#!/usr/bin/env python3
"""Keeps the General board in step with the outside world.

1. GitHub: open issues/PRs of the user's own repos become tasks (deduped by sourceUrl);
   closed or merged ones are moved to Done and completed.
2. Reminders: tasks with a deadline get a reminder in the iCloud "Projects" list
   (never Exchange). Matched by name, so reruns do not duplicate.
"""
import json, subprocess, sys, urllib.request
from pathlib import Path

API = "http://127.0.0.1:1725"
HERE = Path(__file__).parent
CODE = Path.home() / "Projects/code"
OWNER = "your-github-user"
GOAL = "General"
REMINDER_PREFIX = "TaskView: "


def creds():
    return dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json"}
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req))


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def main():
    c = creds()
    tok = call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]

    goal = next(g for g in api("GET", "/module/goals") if g["name"] == GOAL)
    gid = goal["id"]
    statuses = {s["name"]: s["id"] for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": gid})}
    lists = {l["name"]: l["id"] for l in api("GET", f"/module/goal_lists/{gid}")}

    tasks = []
    for sid in statuses.values():
        r = api("GET", f"/module/kanban/tasks/{gid}/{sid}/null")
        tasks += r["tasks"]
        while r["nextCursor"]:
            r = api("GET", f"/module/kanban/tasks/{gid}/{sid}/{r['nextCursor']}")
            tasks += r["tasks"]
    by_url = {t["sourceUrl"]: t for t in tasks if t.get("sourceUrl")}

    # 1. GitHub issues and PRs
    for name, lid in lists.items():
        remote = sh("git", "-C", str(CODE / name), "remote", "get-url", "origin")
        if f"github.com/{OWNER}/" not in remote:
            continue
        repo = remote.split("github.com/")[1].removesuffix(".git")
        for kind, flag in (("issue", "--state"), ("pr", "--state")):
            out = sh("gh", kind, "list", "-R", repo, flag, "all", "--limit", "100", "--json", "number,title,url,state")
            for it in json.loads(out or "[]"):
                is_open = it["state"] == "OPEN"
                t = by_url.get(it["url"])
                if t is None and is_open:
                    api("POST", "/module/tasks", {"goalId": gid, "goalListId": lid, "statusId": statuses["TODO"],
                                                  "description": f"[{kind} #{it['number']}] {it['title']}", "sourceUrl": it["url"]})
                    print("added", it["url"])
                elif t is not None and not is_open and not t["complete"]:
                    api("PATCH", "/module/tasks", {"id": t["id"], "complete": True, "statusId": statuses["Done"]})
                    print("closed", it["url"])

    # 2. Reminders (iCloud only)
    have = sh("osascript", "-e", 'tell application "Reminders" to get name of every reminder of list "Projects" of account "iCloud"')
    if have == "" and "Projects" not in sh("osascript", "-e", 'tell application "Reminders" to get name of every list of account "iCloud"'):
        print("iCloud list 'Projects' not found; skipping reminders")
        return
    for t in tasks:
        if t["complete"] or not t.get("endDate"):
            continue
        title = REMINDER_PREFIX + t["description"]
        if title in have:
            continue
        y, m, d = t["endDate"][:10].split("-")
        title_esc = title.replace('"', "'")
        sh("osascript", "-e", f'''tell application "Reminders"
set dd to current date
set year of dd to {int(y)}
set month of dd to {int(m)}
set day of dd to {int(d)}
set time of dd to 9 * hours
make new reminder at end of reminders of list "Projects" of account "iCloud" with properties {{name:"{title_esc}", due date:dd}}
end tell''')
        print("reminder", title)


if __name__ == "__main__":
    sys.exit(main())
