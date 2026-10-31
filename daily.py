#!/usr/bin/env python3
"""Morning digest + due-date reminders, sent to the phone through ntfy.

- Tasks tagged `para-mí` (not done) due today or overdue.
- What is waiting in Review and what the AI dispatcher did since yesterday.
Also prunes finished ai/ worktrees and branches (ai-clean.py --remove).
Run daily from launchd (com.example.taskview-daily).
"""
import json, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json"}
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))

def token(c):
    """Scoped API token (selfhost/.api-token, revocable from the web UI) when present, else the password login."""
    f = HERE / ".api-token"
    return f.read_text().strip() if f.exists() else call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]


def main():
    # housekeeping: drop worktrees and ai/ branches of finished work (merged PRs / Done tasks)
    subprocess.run([sys.executable, str(HERE / "ai-clean.py"), "--remove"], capture_output=True)
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = token(c)
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    today = time.strftime("%Y-%m-%d")
    alltags = api("GET", "/module/tags")
    due, review, ai, top, asks = [], [], 0, [], 0
    for g in api("GET", "/module/goals"):
        if g.get("archive"):
            continue
        mine = next((t["id"] for t in alltags if t["goalId"] == g["id"] and t["name"] == "para-mí"), None)
        aitags = {t["id"] for t in alltags if t["goalId"] == g["id"] and t["name"] in ("ia·plan-listo", "ia·hecho", "ia·fallo")}
        askt = next((t["id"] for t in alltags if t["goalId"] == g["id"] and t["name"] == "ia·pregunta"), None)
        for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": g["id"]}):
            if s["name"] == "Done":
                continue
            for t in api("GET", f"/module/kanban/tasks/{g['id']}/{s['id']}/null")["tasks"]:
                end = (t.get("endDate") or "")[:10]
                try:
                    time.strptime(end, "%Y-%m-%d")
                    if len(end) != 10:
                        end = ""
                except ValueError:
                    end = ""
                if mine in (t.get("tags") or []) and end and end <= today:
                    due.append(f"{t['description']} ({g['name']}{', vencida' if end < today else ''})")
                if mine in (t.get("tags") or []) and s["name"] in ("TODO", "In Progress"):
                    top.append((end or "9999", -(t.get("priorityId") or 1), t["description"], g["name"]))
                if askt in (t.get("tags") or []):
                    asks += 1
                if s["name"] == "Review":
                    review.append(t["description"])
                    if aitags & set(t.get("tags") or []):
                        ai += 1
    lines = []
    if due:
        lines.append("Para hoy:\n- " + "\n- ".join(due[:8]))
    top.sort()
    if top:  # most urgent first: earliest date, then highest priority
        lines.append("Top 3 tuyas:\n- " + "\n- ".join(f"{d} ({g})" for _, _, d, g in top[:3]))
    if asks:
        lines.append(f"La IA espera tu respuesta en {asks} tarjeta(s)")
    if review:
        lines.append(f"En Review: {len(review)}" + (f" ({ai} de la IA)" if ai else ""))
    if not lines:
        return
    title = f"Buenos días: {len(due)} para hoy" if due else "Buenos días"
    subprocess.run([sys.executable, str(HERE / "notify.py"), title, "\n".join(lines), "3"])


if __name__ == "__main__":
    main()
