#!/usr/bin/env python3
"""Weekly review, sent by email and as a notification on Sunday evening (launchd com.example.taskview-weekly).

Read-only: queries Postgres through `docker exec` and parses selfhost/logs/ai-dispatch.log.
- Closed this week per project, split own (assigned to the user) vs. the rest.
- Stuck: Review or AI-question cards untouched for 7+ days.
- AI runs of the last 7 days and the question rate.
- Next week: due dates and the user's open priorities.
- Health: newest backup.
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

import ia_users

HERE = Path(__file__).resolve().parent
MAIL_TO = "you@example.com"
HIMALAYA_DIR = "/opt/homebrew/bin"
DOCKER = "/usr/local/bin/docker" if Path("/usr/local/bin/docker").exists() else "/opt/homebrew/bin/docker"
BASELINE_ASK_RATE = 9 / 23


def owner_email():
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    return ia_users.owner_email(c)


def mine_sql():
    """SQL condition: task `t` is assigned to the user."""
    e = owner_email().replace("'", "''")
    return f"""EXISTS (SELECT 1 FROM tasks_auth.task_assignee ta JOIN collaboration.users cu ON cu.id = ta.collab_user_id
                     WHERE ta.task_id = t.id AND lower(cu.email) = '{e}')"""


def sql(q):
    p = subprocess.run([DOCKER, "exec", "taskview-db", "psql", "-U", "taskview_db_user", "-d", "taskviewdb",
                        "-At", "-F", "\t", "-c", q], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-200:])
    return [l.split("\t") for l in p.stdout.splitlines() if l]


def block(head, items, limit=15):
    if not items:
        return ""
    more = f"\n- ... y {len(items) - limit} más" if len(items) > limit else ""
    return f"{head} ({len(items)})\n" + "\n".join(f"- {i}" for i in items[:limit]) + more


def closed_this_week():
    rows = sql(f"""
      SELECT g.name, t.description, {mine_sql()}
      FROM tasks.tasks t JOIN tasks.goals g ON g.id = t.goal_id
      WHERE t.complete AND t.date_complete > now() - interval '7 days' AND COALESCE(g.archive, 0) = 0
      ORDER BY g.name, t.date_complete""")
    mine = [f"{d} ({g})" for g, d, m in rows if m == "t"]
    other = [f"{d} ({g})" for g, d, m in rows if m != "t"]
    return mine, other


def stuck():
    # Review cards and AI questions whose last change is older than 7 days. edit_date is the last edit of the task.
    rows = sql("""
      SELECT g.name, t.description, s.name, COALESCE(t.edit_date, t.date_creation)::date
      FROM tasks.tasks t JOIN tasks.goals g ON g.id = t.goal_id
      LEFT JOIN tasks.statuses s ON s.id = t.status_id
      WHERE NOT COALESCE(t.complete, false) AND COALESCE(g.archive, 0) = 0
        AND COALESCE(t.edit_date, t.date_creation) < now() - interval '7 days'
        AND (s.name = 'Review' OR EXISTS (SELECT 1 FROM tasks.tasks_to_tags tt JOIN tasks.tags tg ON tg.id = tt.tag_id
                                          WHERE tt.task_id = t.id AND tg.name = 'ia·pregunta'))
      ORDER BY 4""")
    return [f"{d} ({g}, {s or 'Backlog'}, desde {when})" for g, d, s, when in rows]


def ai_runs():
    log = HERE / "logs/ai-dispatch.log"
    if not log.exists():
        return None
    since = datetime.now() - timedelta(days=7)
    ends, by_model = [], {}
    for line in log.read_text(errors="replace").splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) end t\d+ ok=(\w+) (\S+)(?: runner=(\S+) model=(\S+) secs=(\d+))?", line)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= since:
            ends.append((m.group(2) == "True", m.group(3)))
            if m.group(5):
                by_model.setdefault(m.group(5), []).append((m.group(3), int(m.group(6))))
    total = len(ends)
    asked = sum(1 for _, tag in ends if tag.endswith("pregunta"))
    failed = sum(1 for ok, tag in ends if not ok or tag.endswith("fallo"))
    return total, asked, failed, by_model


def local_usage():
    """local-model calls in the last 7 days from its usage.jsonl."""
    f = Path.home() / "Projects/code/local-model/usage.jsonl"
    if not f.exists():
        return ""
    since, rows = time.time() - 7 * 86400, []
    for line in f.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) >= since:
            rows.append(r)
    if not rows:
        return ""
    bad = sum(1 for r in rows if not r.get("ok"))
    return f"local-model: {len(rows)} llamadas, {bad} con error, {sum(r['secs'] for r in rows) / len(rows):.0f} s de media ({rows[-1]['model']})"


def next_week():
    rows = sql("""
      SELECT g.name, t.description, t.end_date, COALESCE(t.priority_id, 1)
      FROM tasks.tasks t JOIN tasks.goals g ON g.id = t.goal_id
      WHERE NOT COALESCE(t.complete, false) AND COALESCE(g.archive, 0) = 0
        AND t.end_date BETWEEN current_date AND current_date + 7
      ORDER BY t.end_date, 4 DESC""")
    due = [f"{e} {d} ({g})" for g, d, e, _ in rows]
    top = sql(f"""
      SELECT g.name, t.description, COALESCE(t.priority_id, 1)
      FROM tasks.tasks t JOIN tasks.goals g ON g.id = t.goal_id
      LEFT JOIN tasks.statuses s ON s.id = t.status_id
      WHERE NOT COALESCE(t.complete, false) AND COALESCE(g.archive, 0) = 0
        AND (s.name IN ('TODO', 'In Progress'))
        AND {mine_sql()}
      ORDER BY 3 DESC, t.end_date NULLS LAST LIMIT 5""")
    return due, [f"{d} ({g})" for g, d, _ in top]


def backup_line():
    files = sorted((Path.home() / "Backups/taskview").glob("taskview-*.sql.gz"))
    if not files:
        return "Backup: ninguno encontrado"
    age = (time.time() - files[-1].stat().st_mtime) / 3600
    return f"Backup: {files[-1].name}, hace {age:.0f} h" + (" (ATENCIÓN: más de 36 h)" if age > 36 else "")


def build():
    mine, other = closed_this_week()
    parts = [block("Cerradas por ti esta semana", mine), block("Cerradas por la IA u otros", other)]
    s = stuck()
    parts.append(block("Atascadas más de 7 días (Review o pregunta de la IA)", s))
    runs = ai_runs()
    if runs and runs[0]:
        total, asked, failed, by_model = runs
        parts.append(f"IA esta semana\n- {total} ejecuciones, {asked} con pregunta ({asked / total:.0%}; antes {BASELINE_ASK_RATE:.0%}), {failed} con fallo")
        for m, rs in sorted(by_model.items()):
            good = sum(1 for tag, _ in rs if tag.endswith(("hecho", "plan-listo")))
            parts[-1] += f"\n- {m}: {len(rs)} ejecuciones, {good} hechas/plan listo, {sum(1 for tag, _ in rs if tag.endswith('fallo'))} fallo, {sum(sec for _, sec in rs) // 60} min"
        loc = local_usage()
        if loc:
            parts[-1] += "\n- " + loc
    due, top = next_week()
    parts += [block("Vence en 7 días", due), block("Tus prioridades abiertas", top, 5), backup_line()]
    body = "\n\n".join(p for p in parts if p)
    summary = f"{len(mine) + len(other)} cerradas, {len(s)} atascadas, {len(due)} vencen"
    return summary, body


def main():
    summary, body = build()
    title = f"Resumen semanal: {summary}"
    subprocess.run([sys.executable, str(HERE / "notify.py"), title, body[:700], "3"])
    env = {**os.environ, "PATH": f"{HIMALAYA_DIR}:{os.environ.get('PATH', '')}"}
    p = subprocess.run([str(Path.home() / ".local/bin/send-mail"), "--to", MAIL_TO, "--subject", f"TaskView: {title}", "--text", body],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print("weekly mail failed:", (p.stdout + p.stderr).strip()[-300:], file=sys.stderr)


if __name__ == "__main__":
    if "--print" in sys.argv:
        print(build()[1])
    else:
        main()
