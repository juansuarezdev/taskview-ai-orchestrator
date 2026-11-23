#!/usr/bin/env python3
"""Morning digest + due-date reminders, sent to the phone as a notification (notify.py).

- Tasks assigned to the user (not done) due today or overdue.
- What is waiting in Review and what the AI dispatcher did since yesterday.
Also emails a fuller version (full lists, titles of AI results and questions) with send-mail.
Also prunes finished ai/ worktrees and branches (ai-clean.py --remove).
Run daily from launchd (<prefix>.taskview-daily).
"""
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path

import ia_users
import orq_site

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"
MAIL_TO = orq_site.SITE["mail_to"]
HIMALAYA_DIR = "/opt/homebrew/bin"  # launchd has a minimal PATH


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json"}
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))

def token(c):
    """Scoped API token (.api-token, revocable from the web UI) when present, else the password login."""
    f = HERE / ".api-token"
    return f.read_text().strip() if f.exists() else call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]


def main():
    # housekeeping: drop worktrees and ai/ branches of finished work (merged PRs / Done tasks)
    subprocess.run([sys.executable, str(HERE / "ai-clean.py"), "--remove"], capture_output=True)
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = token(c)
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    owner = ia_users.owner_email(c)
    today = time.strftime("%Y-%m-%d")
    alltags = api("GET", "/module/tags")
    due, review, ai, top, asks = [], [], 0, [], 0
    ai_done, ai_asks = [], []  # titles for the email
    for g in api("GET", "/module/goals"):
        if g.get("archive"):
            continue
        collab = ia_users.collaborators(api, g["id"])
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
                mine = owner in ia_users.assignee_emails(t, collab)
                if mine and end and end <= today:
                    due.append(f"{t['description']} ({g['name']}{', vencida' if end < today else ''})")
                if mine and s["name"] in ("TODO", "In Progress"):
                    top.append((end or "9999", -(t.get("priorityId") or 1), t["description"], g["name"]))
                if askt in (t.get("tags") or []):
                    asks += 1
                    ai_asks.append(f"{t['description']} ({g['name']})")
                if s["name"] == "Review":
                    review.append(t["description"])
                    if aitags & set(t.get("tags") or []):
                        ai += 1
                        ai_done.append(f"{t['description']} ({g['name']})")
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
    mail_digest(title, due, top, ai_asks, ai_done, review)


def mail_digest(title, due, top, ai_asks, ai_done, review):
    """One morning email with everything, instead of waiting for one mail per task. Failure is logged, never raised."""
    def block(head, items):
        return f"{head} ({len(items)})\n" + "\n".join(f"- {i}" for i in items) if items else ""
    parts = [
        block("Para hoy o vencidas", due),
        block("Top 3 tuyas", [f"{d} ({g})" for _, _, d, g in top[:3]]),
        block("La IA espera tu respuesta", ai_asks),
        block("Resultados de la IA en Review", ai_done),
        block("Todo lo que hay en Review", review),
    ]
    body = "\n\n".join(p for p in parts if p)
    if not MAIL_TO:
        return  # no mail_to in orq-site.json / ORQ_MAIL_TO: notification only
    env = {**os.environ, "PATH": f"{HIMALAYA_DIR}:{os.environ.get('PATH', '')}"}
    p = subprocess.run([str(Path.home() / ".local/bin/send-mail"), "--to", MAIL_TO, "--subject", f"TaskView: {title}", "--text", body],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print("digest mail failed:", (p.stdout + p.stderr).strip()[-300:], file=sys.stderr)


if __name__ == "__main__":
    main()
