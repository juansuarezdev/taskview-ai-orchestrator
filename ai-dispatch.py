#!/usr/bin/env python3
"""Run TaskView tasks tagged for an AI agent.

A task (status Backlog/TODO) runs when it is assigned to an AI user AND carries a mode tag.
The assignee decides WHO (ia-users.json): Claude -> claude, OpenCode -> OpenCode (free model, Opus still reviews); both -> Claude.
The tag decides WHAT:
  ia·plan      -> plans only (read-only), writes the plan into the task note, moves it to Review
  ia·ejecutar  -> implements it in its own git worktree/branch `ai/t<id>`, never pushes, moves it to Review
A mode tag with no AI assignee does nothing. Tasks assigned to the user (owner in ia-users.json) are never touched.
If Claude hits the 5-hour session limit during exec (or a cooldown file says so), the same task continues with OpenCode automatically; Opus review is still deferred on limit.
Models: plan = Opus, exec = Sonnet, then an automatic review by Opus (ia·hecho if approved, ia·cambios if not).
Plan/exec sessions delegate to subagents by model: `explorer` (Haiku, read-only search) and `tester` (Sonnet, runs tests).
The review answers with a JSON schema (verdict + findings) instead of free text.
Questions: a claude session that lacks information ends with `PREGUNTA: ...`; the task goes to Review with ia·pregunta and the
session id in the note. Answer by writing `RESPUESTA: ...` below the `Sesión IA:` line and tag it ia·continuar: the session is resumed.
Progress tags: ia·trabajando while running, then ia·plan-listo / ia·hecho / ia·cambios / ia·fallo / ia·pregunta.
Packs (ia-packs.json): ia·pack·analizar / implementar / barato / silencioso = mode + email in one tag
(barato is plain ia·ejecutar + mail: the cheap runner comes from assigning OpenCode).
Tag ia·correo on a task: when its run ends the result is emailed (send-mail) and the tag is removed.
One task at a time (flock). Run every minute from launchd. `--dry-run` only lists.
"""
import base64, fcntl, html, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

import ia_users

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"
CODE = Path.home() / "Projects/code"
EXTRA_ROOTS = {"repo-r": Path.home() / "Projects/repo-r",  # boards whose folder is not ~/Projects/code/<repo>; no git repo there = plan only
               "inbox": CODE,  # loose tasks: read-only analysis across every repo
               "repo-h": Path.home() / "vault/projects/project"}
WORKTREES = CODE / ".ai-worktrees"
LOG = HERE / "logs/ai-dispatch.log"
REPOS = {"Board M": "repo-m", "Board G": "repo-g", "Board E": "repo-e", "TaskView": "taskview-community", "Board R": "repo-r", "Board GC": "repo-gc", "Board SB": "repo-sb", "local-model": "local-model",
         "Board A": "repo-a", "Board H": "repo-h", "Inbox": "inbox", "Board C": "repo-c"}
TRIGGERS = {"ia·plan": "plan", "ia·ejecutar": "exec"}  # mode tag -> mode; the runner comes from the assignee
# packs (ia-packs.json): one tag = a mode tag + optional mail, so tasks need a single tag; they win over loose triggers
PACKS = json.loads((HERE / "ia-packs.json").read_text()) if (HERE / "ia-packs.json").exists() else {}
TRIGGERS = {**{n: TRIGGERS[p["trigger"]] for n, p in PACKS.items()}, **TRIGGERS}
MAIL = "ia·correo"  # per-task: email the result to MAIL_TO when the run ends, then the tag is removed
MAIL_TO = "you@example.com"
HIMALAYA_DIR = "/opt/homebrew/bin"  # launchd PATH does not include it
CONTINUE = "ia·continuar"  # set by the user on a Review task after writing RESPUESTA: in the note
PROGRESS = {"ia·trabajando": "#f59e0b", "ia·plan-listo": "#3b82f6", "ia·hecho": "#22c55e", "ia·cambios": "#eab308", "ia·fallo": "#ef4444",
            "ia·pregunta": "#a855f7"}
USER_TAGS = {"flujo-ia": "#14b8a6"}  # created on every served board too: flujo-ia = Bandeja IA list
TRIGGER_COLORS = {"ia·plan": "#3b82f6", "ia·ejecutar": "#f97316", CONTINUE: "#a855f7", MAIL: "#0ea5e9",
                  **{n: p["color"] for n, p in PACKS.items()}}
MODELS = {"plan": "claude-opus-5-5", "exec": "claude-sonnet-5-5", "review": "claude-opus-5-5",
          "explore": "claude-haiku-5-5", "test": "claude-sonnet-5-5"}  # explore/test = subagent models
EXPLORER = {"description": "Busca archivos, símbolos y usos en el repo. Úsalo para localizar código antes de editar o planificar.",
            "prompt": "Eres un buscador de código de solo lectura. Localiza lo que te pidan y responde con rutas, líneas y fragmentos mínimos. No propongas cambios.",
            "tools": ["Read", "Glob", "Grep"], "model": MODELS["explore"]}
TESTER = {"description": "Ejecuta los tests, el build o el linter del repo y resume los fallos. Úsalo para verificar cambios.",
          "prompt": "Ejecuta lo que te pidan (tests, build, lint) y devuelve solo: comando, resultado y las líneas decisivas de cada fallo. No edites archivos.",
          "tools": ["Read", "Glob", "Grep", "Bash"], "model": MODELS["test"]}
PLAN_AGENTS = {"explorer": EXPLORER}
EXEC_AGENTS = {"explorer": EXPLORER, "tester": TESTER}
DELEGATE = "Delega la búsqueda de código en el subagente `explorer`" + " y la ejecución de tests/build en el subagente `tester`; tú decides y editas.\n"
ASK_RULE = ("Si te falta información imprescindible que no puedes deducir del repo ni de la tarea, no adivines: termina tu respuesta con una línea "
            "`PREGUNTA: <una pregunta concreta>` y no hagas más cambios.\n")
REVIEW_SCHEMA = {"type": "object", "required": ["veredicto", "resumen", "hallazgos"], "properties": {
    "veredicto": {"enum": ["APROBADO", "CAMBIOS"]}, "resumen": {"type": "string"},
    "hallazgos": {"type": "array", "items": {"type": "object", "required": ["archivo", "severidad", "descripcion"], "properties": {
        "archivo": {"type": "string"}, "linea": {"type": "integer"}, "severidad": {"enum": ["alta", "media", "baja"]},
        "descripcion": {"type": "string"}}}}}}
SESSION_RE = re.compile(r"Sesión IA: (\S+) mode=(\w+) runner=(\w+)")
CONTEXT_MIN = 80  # chars of note below which the task gets a pre-context pass
MAX_ROUNDS = 3  # execute + review rounds when the review asks for changes
BUDGET = {"plan": "2", "exec": "5", "review": "2"}
TIMEOUT = {"plan": 15 * 60, "exec": 40 * 60, "review": 15 * 60}
_ALLOWED_BASH = ["git status", "git diff", "git log", "git show", "git add", "git commit", "ls", "cat", "head", "tail", "wc", "grep", "find", "which",
                 "npm", "npx", "pnpm", "python3", "node", "send-mail", "composer install", "composer dump-autoload",
                 "php -l", "php -v", "php artisan test", "php vendor/bin/phpunit", "php vendor/bin/pest", "vendor/bin/pint", "php vendor/bin/pint"]
_DENIED_BASH = ["git push", "rm", "curl", "sudo", "git reset", "git checkout main", "git merge", "git rebase", "git switch main",
                "git commit --no-verify", "git commit -n"]
# The user's RTK PreToolUse hook rewrites commands before the permission check (`git add` -> `rtk git add`, `cat`/`head` -> `rtk read`,
# `vendor/bin/pint` -> `rtk pint`), so every allow AND deny rule also needs its rtk form; without it headless sessions were denied even
# `git add -A` (2026-11-14, SiteApp t368-t379) and `git push` slipped past the deny list as `rtk git push`.
_RTK_ALIAS = {"cat": "read", "head": "read", "vendor/bin/pint": "pint", "php vendor/bin/pint": "pint"}


def _bash_rules(cmds):
    out = []
    for c in cmds:
        out += [f"Bash({c}:*)", f"Bash(rtk {_RTK_ALIAS.get(c, c)}:*)"]
    return list(dict.fromkeys(out))


ALLOWED = ["Read", "Glob", "Grep", "Edit", "Write", *_bash_rules(_ALLOWED_BASH)]
WEB = ["WebSearch", "WebFetch"]  # analysis (plan) sessions may research the web
DENIED = _bash_rules(_DENIED_BASH)


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")
    m = re.match(r"(start|end) t(\d+)", msg)
    if m:  # same event into the shared registry (vault-log -> events.jsonl)
        try:
            subprocess.run([str(Path.home() / "vault/bin/vault-log"), "--agent", "dispatcher", "--kind", m.group(1), "--task", "t" + m.group(2), msg], check=False, timeout=10)
        except Exception:
            pass


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json", "x-taskview-client": "ai-dispatch"}  # history/Recientes record these changes as the AI's
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))

def token(c):
    """Scoped API token (selfhost/.api-token, revocable from the web UI) when present, else the password login."""
    f = HERE / ".api-token"
    return f.read_text().strip() if f.exists() else call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]


def select(tag_names, assignees, owner):
    """What to run for one task: (trigger tag, mode, runner) or None.
    tag_names: names of the task's tags; assignees: emails of its assignees; owner: the user's email.
    Assigned to the user -> never. Needs a mode tag (packs first) AND an AI assignee, which picks the runner."""
    if owner and owner in assignees:
        return None
    trig = next((n for n in TRIGGERS if n in tag_names), None)
    runner = ia_users.runner_of(assignees)
    if not trig or not runner:
        return None
    return trig, TRIGGERS[trig], runner


def main():
    dry = "--dry-run" in sys.argv
    lock = open(HERE / "logs/.ai-dispatch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = token(c)
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    owner = ia_users.owner_email(c)
    if not owner:  # without it the user's own tasks could be picked up
        log("no owner email (ia-users.json owner / .credentials email): nothing runs")
        return

    for g in api("GET", "/module/goals"):
        repo = REPOS.get(g["name"])
        if not repo or g.get("archive"):
            continue
        gid = g["id"]
        # the endpoint ignores ?goalId and returns every goal's tags, and toggling needs the tag of the task's own goal
        tags = {t["name"]: t["id"] for t in api("GET", f"/module/tags?goalId={gid}") if t["goalId"] == gid}
        for name, color in {**TRIGGER_COLORS, **PROGRESS, **USER_TAGS}.items():  # make the tags exist so they can be picked in the UI
            if name not in tags and not dry:
                tags[name] = api("POST", "/module/tags", {"name": name, "color": color, "goalId": gid})["id"]
        if not any(n in tags for n in (*TRIGGERS, CONTINUE)):
            continue
        st = {s["name"]: s["id"] for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": gid})}
        collab = ia_users.collaborators(api, gid)
        names = {v: k for k, v in tags.items()}
        for sname in ("TODO", "Backlog", "Review"):
            col = "null" if sname == "Backlog" else st.get(sname)  # Backlog is not a status row: tasks with no status, addressed as `null`
            if col is None:
                continue
            cur = "null"
            while cur:
                r = api("GET", f"/module/kanban/tasks/{gid}/{col}/{cur}")
                for t in r["tasks"]:
                    have = set(t.get("tags") or [])
                    who = ia_users.assignee_emails(t, collab)
                    if owner in who or t.get("complete"):  # the user's own task; done tasks may keep a stale trigger tag
                        continue
                    if sname == "Review":  # only answered questions are picked up from Review
                        if tags.get(CONTINUE) not in have:
                            continue
                        res, err = parse_resume(t.get("note") or "")
                        if dry:
                            print(f"[{g['name']}] t{t['id']} continuar: {err or res['mode'] + '/' + res['runner']}")
                        elif err:
                            toggle(api, tags, t, CONTINUE, False)
                            log(f"continue t{t['id']} skipped: {err}")
                            subprocess.run([sys.executable, str(HERE / "notify.py"), f"IA: {t['description'][:60]}", f"No puedo continuar: {err}", "3", "--task", str(t["id"])])
                        else:
                            run(api, g, repo, st, tags, t, res["mode"], CONTINUE, res["runner"], res)
                            return
                        continue
                    hit = select({names[i] for i in have if i in names}, who, owner)
                    if hit:
                        trig, mode, runner = hit
                        if dry:
                            print(f"[{g['name']}] t{t['id']} {mode}/{runner}: {t['description']}")
                        else:
                            run(api, g, repo, st, tags, t, mode, trig, runner)
                            return  # one task per tick
                cur = str(r["nextCursor"]) if r.get("nextCursor") else ""


def toggle(api, tags, t, name, on):
    has = tags[name] in set(t.get("tags") or [])
    if has != on:
        api("PATCH", "/module/tags/toggle", {"tagId": tags[name], "taskId": t["id"]})
        t["tags"] = (list(t.get("tags") or []) + [tags[name]]) if on else [x for x in t["tags"] if x != tags[name]]


def board_context(api, goal, st, t):
    """Board name/description plus the titles of sibling tasks, so the model knows what the project is about."""
    sib = []
    for name in ("In Progress", "Review", "TODO", "Backlog"):
        if name in st:
            for x in api("GET", f"/module/kanban/tasks/{goal['id']}/{st[name]}/null")["tasks"]:
                if x["id"] != t["id"] and len(sib) < 15:
                    sib.append(f"- [{name}] {x['description']}")
    desc = (goal.get("description") or "").strip()
    return (f"Proyecto (board de TaskView): {goal['name']}\n" + (f"Descripción del proyecto: {desc}\n" if desc else "")
            + ("Otras tareas del proyecto (solo contexto, no son tu tarea):\n" + "\n".join(sib) + "\n" if sib else ""))


def parse_resume(note):
    """Session id/mode/runner from the last `Sesión IA:` line plus the user's `RESPUESTA:` below it; returns (dict, error)."""
    found = list(SESSION_RE.finditer(note))
    if not found:
        return None, "no hay línea 'Sesión IA:' en la nota"
    m = found[-1]
    # the web note editor saves HTML: turn it back into plain text before looking for the answer
    tail = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</p>|<br\s*/?>", "\n", note[m.end():])))
    answers = re.findall(r"RESPUESTA:\s*(.+?)(?=\n\s*RESPUESTA:|\Z)", tail, re.S)
    a = answers[-1].strip() if answers else None
    if not a:
        return None, "falta 'RESPUESTA: ...' debajo de la línea 'Sesión IA:'"
    return {"sid": m.group(1), "mode": m.group(2), "runner": m.group(3), "answer": a}, None


def question(text):
    """The model's `PREGUNTA:` line (and what follows), or None."""
    found = list(re.finditer(r"^\W*PREGUNTA:\s*", text, re.M))
    return text[found[-1].end():].strip() if found else None


def fmt_review(so):
    lines = [f"VEREDICTO: {so['veredicto']}", so.get("resumen", "")]
    for h in so.get("hallazgos", []):
        lines.append(f"- [{h['severidad']}] {h['archivo']}{':' + str(h['linea']) if h.get('linea') else ''}: {h['descripcion']}")
    return "\n".join(lines)


CURRENT_TASK = ""  # id of the task being run; child sessions get it as TASKVIEW_TASK_ID
RUN_ENV = {}  # extra env for the sessions of the task being run (test DB, PHP memory); set in run()
HERD_BIN = str(Path.home() / ".config/herd-lite/bin")  # php/composer; launchd PATH does not include it
TEST_ENV_REPOS = {"repo-c"}  # Laravel+Sail repos: `php artisan test` in the worktree against the Sail Postgres `testing` DB
TEST_HINT = ("\nTests: el dispatcher ya dejó vendor/, node_modules/ y public/build listos (si faltan, `composer install`, `npm ci` y `npm run build`). Ejecuta `php artisan test` (o `php vendor/bin/phpunit`): el entorno ya apunta a la BD `testing` "
             "de Sail (Postgres; SQLite no sirve por btree_gist). No uses sail up, ni migraciones contra la BD de desarrollo, ni toques .env. "
             "Commits con `git add` y `git commit` solo en tu rama ai/t<id>; nunca push, merge, rebase ni cambiar a main.\n")


def repo_test_env(base):
    """Env so tests in a worktree use the Sail Postgres testing DB via its host port, without a .env in the worktree."""
    try:
        e = {k.strip(): v.strip().strip('"') for k, v in (l.split("=", 1) for l in (base / ".env").read_text().splitlines() if "=" in l and not l.lstrip().startswith("#"))}
    except Exception:
        return {}
    return {"DB_CONNECTION": "pgsql", "DB_HOST": "127.0.0.1", "DB_PORT": e.get("FORWARD_DB_PORT") or "5432", "DB_DATABASE": "testing",
            "DB_USERNAME": e.get("DB_USERNAME", "sail"), "DB_PASSWORD": e.get("DB_PASSWORD", ""),
            "APP_KEY": "base64:" + base64.b64encode(os.urandom(32)).decode(), "PHP_INI_SCAN_DIR": f":{HERE / 'php-ini'}"}  # memory_limit=1G (dompdf)


def prepare_worktree(wt):
    """Install what tests need in a fresh worktree (vendor/, node_modules/, public/build), so the session can run them.
    Done by the dispatcher, not the model: sessions burned their budget on this and then could not get approval. Returns (ok, detail)."""
    env = {**os.environ, "PATH": f"{HERD_BIN}:{os.environ.get('PATH', '')}", "COMPOSER_NO_INTERACTION": "1"}
    steps = []
    if (wt / "composer.json").exists() and not (wt / "vendor").exists():
        steps.append(["composer", "install", "-q", "--no-interaction", "--no-progress"])
    if (wt / "package-lock.json").exists() and not (wt / "node_modules").exists():
        steps.append(["npm", "ci", "--no-audit", "--no-fund", "--loglevel=error"])
    if (wt / "package.json").exists() and not (wt / "public/build/manifest.json").exists():
        steps.append(["npm", "run", "build", "--silent"])
    for cmd in steps:
        p = subprocess.run(cmd, cwd=wt, env=env, capture_output=True, text=True, timeout=15 * 60)
        if p.returncode != 0:
            return False, f"`{' '.join(cmd)}` falló:\n{(p.stderr or p.stdout)[-1500:]}"
    return True, ", ".join(" ".join(c[:2]) for c in steps) or "ya preparado"


def commit_message(tid, title, attempt):
    """Conventional subject cut at a word boundary (the old one was description[:72], cut mid-word)."""
    head = f"chore(t{tid}): "
    tail = f" (ronda {attempt})" if attempt > 1 else ""
    room = 72 - len(head) - len(tail)
    text = title if len(title) <= room else title[:room].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return head + text + tail


def ask(prompt, perm, model, mode, cwd, agents=None, schema=None, resume=None):
    """Run one claude -p session; returns (ok, text, session_id, structured_output)."""
    extra = [*(["--agents", json.dumps(agents)] if agents else []), *(["--json-schema", json.dumps(schema)] if schema else []),
             *(["--resume", resume] if resume else [])]
    try:
        p = subprocess.run(["claude", "-p", prompt, "--model", model, "--permission-mode", perm, "--max-budget-usd", BUDGET[mode],
                            "--output-format", "json", *extra,
                            "--allowedTools", *ALLOWED, *(WEB if perm == "plan" else []), "--disallowedTools", *DENIED],
                           cwd=cwd, env={**os.environ, "PATH": f"{Path.home()}/.local/bin:{HERD_BIN}:{HIMALAYA_DIR}:{os.environ.get('PATH', '')}", "TASKVIEW_NOHOOK": "1", "TASKVIEW_TASK_ID": CURRENT_TASK, "AI_DISPATCH": "1", **RUN_ENV}, capture_output=True, text=True, timeout=TIMEOUT[mode])
    except subprocess.TimeoutExpired:
        return False, f"Timeout ({TIMEOUT[mode] // 60} min).", None, None
    except Exception as e:
        return False, f"Error: {e}", None, None
    try:
        o = json.loads(p.stdout)
    except ValueError:  # not JSON (crash before output): keep whatever text there is
        return False, (p.stdout or p.stderr or "").strip(), None, None
    return p.returncode == 0 and not o.get("is_error"), (o.get("result") or "").strip(), o.get("session_id"), o.get("structured_output")


OC_BIN = str(Path.home() / ".opencode/bin/opencode")  # launchd PATH does not include it
_OC_FILE = HERE / ".opencode-model"  # one line, e.g. opencode/nemotron-3-ultra-free; overrides the default below
OC_MODEL = _OC_FILE.read_text().strip() if _OC_FILE.exists() else "opencode/deepseek-v4.1-flash-free"  # free model; no paid OpenCode plan
OC_TIMEOUT = 40 * 60
OC_PERMISSION = {"bash": {"*": "allow", "git push*": "deny", "git reset*": "deny", "rm *": "deny", "sudo*": "deny", "curl*": "deny", "wget*": "deny",
                          "git merge*": "deny", "git rebase*": "deny", "git checkout main*": "deny", "git switch main*": "deny",
                          "*--no-verify*": "deny"},
                 "edit": "allow"}

LIMIT_RE = re.compile(r"5-hour limit|hit your limit|usage limit|rate limit|limit reached", re.I)
CLAUDE_LIMIT_FILE = HERE / "logs/.claude-limit-until"  # unix ts; while now < ts, Claude's ia·ejecutar runs go to OpenCode


def is_claude_limit(text):
    return bool(LIMIT_RE.search(text or ""))


def claude_is_limited():
    try:
        until = float(CLAUDE_LIMIT_FILE.read_text().strip())
    except Exception:
        return False
    if time.time() >= until:
        try:
            CLAUDE_LIMIT_FILE.unlink()
        except Exception:
            pass
        return False
    return True


def mark_claude_limited(text=None, hours=5):
    """Record cooldown until Claude's 5h window resets (default +5h from now)."""
    until = time.time() + hours * 3600
    # best-effort parse of "resets … HH:MM" in local time-local wall clock
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?", text or "", re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ap = (m.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        now = time.localtime()
        cand = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, mi, 0, 0, 0, -1))
        if cand <= time.time():
            cand += 24 * 3600
        until = cand
    CLAUDE_LIMIT_FILE.parent.mkdir(exist_ok=True)
    CLAUDE_LIMIT_FILE.write_text(str(until))
    log(f"claude limit until {time.strftime('%F %T', time.localtime(until))}")


def opencode_ok_check(ok, out, wt, review_base):
    """Exit 0 with no file changes and no commits is a silent failure for OpenCode."""
    if not ok:
        return ok, out
    dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    ahead = subprocess.run(["git", "-C", str(wt), "rev-list", "--count", f"{review_base}..HEAD"], capture_output=True, text=True).stdout.strip()
    if not dirty and ahead in ("", "0"):
        return False, f"{out}\n(OpenCode terminó sin cambios ni commits: se da por fallo. Reintenta o asigna la tarea a Claude.)"
    return ok, out


def ask_opencode(prompt, cwd, readonly=False):
    """Run one `opencode run` session with the same guard rails as the claude executor; returns (ok, text).
    readonly (plan mode): no edits and no shell, only its read/search tools."""
    perm = {"bash": "deny", "edit": "deny"} if readonly else OC_PERMISSION
    try:
        p = subprocess.run([OC_BIN, "run", "--model", OC_MODEL, prompt], cwd=cwd, capture_output=True, text=True,
                           timeout=OC_TIMEOUT, env={**os.environ, "PATH": f"{Path.home()}/.local/bin:{HERD_BIN}:{HIMALAYA_DIR}:{os.environ.get('PATH', '')}", "TASKVIEW_NOHOOK": "1", "TASKVIEW_TASK_ID": CURRENT_TASK, "AI_DISPATCH": "1", **RUN_ENV, "OPENCODE_PERMISSION": json.dumps(perm)})
        return p.returncode == 0, (p.stdout or p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"Timeout ({OC_TIMEOUT // 60} min)."
    except Exception as e:
        return False, f"Error: {e}"


def notify_detail(done_tag, sections):
    """One short line for the phone: the question, the failure cause, the plan's start or the review verdict."""
    flat = lambda x: " ".join((x or "").split())
    if done_tag == "ia·pregunta":
        return "Pregunta: " + flat(question(sections[-1][2]))[:350] if sections and question(sections[-1][2]) else ""
    if done_tag == "ia·fallo":
        bad = [o for _, k, o in sections if not k]
        return "Causa: " + flat(bad[-1])[-300:] if bad else ""
    if done_tag == "ia·plan-listo":
        plan = [o for h, k, o in sections if k and h.startswith("Plan")]
        return "Plan: " + flat(plan[-1])[:300] if plan else ""
    rev = [o for h, k, o in sections if k and h.startswith("Revisión")]
    return flat(rev[-1])[:300] if rev else ""


def run(api, goal, repo, st, tags, t, mode, trig, runner="claude", resume=None):
    tid = t["id"]
    global CURRENT_TASK, RUN_ENV
    CURRENT_TASK = str(tid)
    if mode == "exec" and runner == "claude" and claude_is_limited():
        log(f"t{tid} claude en cooldown → runner=opencode")
        runner = "opencode"
    log(f"start t{tid} {mode} [{goal['name']}] {t['description']}")
    toggle(api, tags, t, trig, False)
    if PACKS.get(trig, {}).get("mail"):
        toggle(api, tags, t, MAIL, True)
    toggle(api, tags, t, "ia·trabajando", True)
    toggle(api, tags, t, "ia·pregunta", False)
    api("PATCH", "/module/tasks", {"id": tid, "statusId": st["In Progress"]})
    ttl = f"IA #{tid} · {goal['name']}: {t['description'][:50]}"
    t_run, who = time.time(), (MODELS["plan"] if mode == "plan" else MODELS["exec"])  # `who` is replaced by the exec branch when OpenCode runs
    subprocess.run([sys.executable, str(HERE / "notify.py"), ttl, f"Empieza ({'plan' if mode == 'plan' else 'ejecución'}, {runner}): pasa a En curso", "2", "--task", str(tid)])
    note = t.get("note") or ""
    base = EXTRA_ROOTS.get(repo, CODE / repo)
    in_place = not (base / ".git").exists()  # no repo: no worktree, read-only analysis only
    branch, wt = f"ai/t{tid}", WORKTREES / f"{repo}-t{tid}"
    mbase0 = re.search(r"(?im)^WORKTREE_BASE:\s*(\S+)", note or "")
    review_base = (mbase0.group(1) if mbase0 else "main")
    ctx = board_context(api, goal, st, t)
    RUN_ENV = repo_test_env(base) if repo in TEST_ENV_REPOS else {}
    ctx += TEST_HINT if RUN_ENV else ""
    ctx += ("\nCorreo: si la tarea pide enviar un correo, usa `send-mail --to you@example.com --subject \"...\" --text \"...\" [--html FILE] [--attach FILE]`. "
            "Solo a ese destinatario; cualquier otro, pregunta antes con PREGUNTA:.\n")
    stamp = lambda: time.strftime("%F %H:%M")
    sections, ok, verdict_ok, asked, review_deferred = [], False, True, None, False  # asked = session id waiting for user
    try:
        if in_place:
            if mode != "plan":
                raise RuntimeError(f"{base} no es un repo git: solo se admite ia·plan (análisis sin cambios)")
            wt = base
        elif not wt.exists():
            WORKTREES.mkdir(exist_ok=True)
            # Optional pin in the task note: "WORKTREE_BASE: <branch>" (e.g. ai/t271). Else main/HEAD.
            mbase = re.search(r"(?im)^WORKTREE_BASE:\s*(\S+)", note or "")
            if mbase:
                start = mbase.group(1)
                chk = subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", start], capture_output=True)
                if chk.returncode != 0:
                    raise RuntimeError(f"WORKTREE_BASE {start} no existe en {base}")
            else:
                start = "main" if subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", "main"], capture_output=True).returncode == 0 else "HEAD"
            review_base = start
            subprocess.run(["git", "-C", str(base), "worktree", "add", "-b", branch, str(wt), start], check=True, capture_output=True)
        if mode == "exec" and repo in TEST_ENV_REPOS and not in_place:
            pok, pdet = prepare_worktree(wt)
            log(f"t{tid} prepare_worktree ok={pok} {pdet[:120]}")
            if not pok:
                sections.append(("Preparación del worktree", False, pdet + "\nLos tests no podrán ejecutarse hasta resolverlo."))
        if not resume and len(re.sub(r"<[^>]+>", " ", note).strip()) < CONTEXT_MIN:  # thin task: a cheap model gathers context first, fewer questions later
            cok, cout, _, _ = ask(f"{ctx}\nTarea de TaskView #{tid}: {t['description']}\n\nLa tarea tiene poca descripción. Explora el repo (solo lectura) y escribe, en español y en "
                                  "máximo 8 líneas, el contexto útil para quien la haga: archivos y módulos relevantes, estado actual y los supuestos razonables que tomarías. "
                                  "No hagas preguntas ni modifiques nada.", "plan", MODELS["explore"], "plan", wt)
            if cok and cout:
                sections.append((f"Contexto previo · {MODELS['explore']}", True, cout))
                note = f"{note}\n\nContexto previo:\n{cout}".strip()
        task_txt = f"Tarea de TaskView #{tid}: {t['description']}\n\nNotas de la tarea:\n{note}\n"
        if mode == "plan":
            if resume:
                prompt = f"Respuesta del usuario a tu pregunta:\n{resume['answer']}\n\nContinúa y entrega el plan completo."
            else:
                prompt = (f"{ctx}\n{task_txt}\nAnaliza el repo y escribe un plan de implementación concreto: archivos a tocar, pasos, riesgos y cómo verificarlo. "
                          f"NO modifiques nada. {DELEGATE.replace(' y la ejecución de tests/build en el subagente `tester`', '')}{ASK_RULE}Responde en español, conciso, en markdown.")
            if runner == "opencode":  # no session resume and no subagents: plain read-only run
                ok, out = ask_opencode(prompt.replace(DELEGATE.replace(' y la ejecución de tests/build en el subagente `tester`', ''), "").replace(ASK_RULE, ""), wt, readonly=True)
                who = f"OpenCode · {OC_MODEL}"
                sections.append((f"Plan de la IA · {who}", ok, out))
            else:
                ok, out, sid, _ = ask(prompt, "plan", MODELS["plan"], "plan", wt, agents=PLAN_AGENTS, resume=resume and resume["sid"])
                sections.append((f"Plan de la IA · {MODELS['plan']}", ok, out))
                if ok and question(out):
                    asked = sid
        else:
            feedback = ""
            for attempt in range(1, MAX_ROUNDS + 1):
                tag = f" · ronda {attempt}" if MAX_ROUNDS > 1 else ""
                exec_prompt = (f"{ctx}\n{task_txt}\n(Las notas pueden incluir un plan ya aprobado: síguelo.)\n{feedback}{DELEGATE}"
                               f"{ASK_RULE if runner == 'claude' else ''}Trabajas en la rama {branch} de un git worktree. Haz commits pequeños con mensaje claro, sin firmas de IA. "
                               "No hagas push, no toques .env ni secretos, no borres nada fuera de lo necesario. Verifica con los tests/build del repo si existen. "
                               "Termina con un resumen en español: qué cambió, cómo verificarlo y qué queda pendiente.")
                sid = None
                if runner == "opencode":
                    ok, out = ask_opencode(exec_prompt, wt)
                    who = f"OpenCode · {OC_MODEL}"
                    ok, out = opencode_ok_check(ok, out, wt, review_base)
                else:
                    if resume and attempt == 1:  # continue the session that asked, with the user's answer
                        exec_prompt = f"Respuesta del usuario a tu pregunta:\n{resume['answer']}\n\nContinúa con la tarea y termina con el resumen."
                    ok, out, sid, _ = ask(exec_prompt, "acceptEdits", MODELS["exec"], "exec", wt, agents=EXEC_AGENTS,
                                          resume=resume["sid"] if resume and attempt == 1 else None)
                    who = MODELS["exec"]
                    if (not ok) and is_claude_limit(out):
                        mark_claude_limited(out)
                        sections.append((f"Resultado de la IA · {who}{tag}", False,
                                         (out or "") + "\n(Límite Claude 5h: paso automático a OpenCode.)"))
                        log(f"t{tid} claude limit mid-exec → opencode")
                        ok, out = ask_opencode(exec_prompt, wt)
                        who = f"OpenCode · {OC_MODEL} (fallback límite Claude)"
                        runner = "opencode"
                        sid = None
                        ok, out = opencode_ok_check(ok, out, wt, review_base)
                sections.append((f"Resultado de la IA · {who}{tag}", ok, out))
                if ok and subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
                    # Safety net for whatever the model left uncommitted; also before a question, so the work is never left only on disk.
                    msg = commit_message(tid, t["description"], attempt)
                    subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
                    c = subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", msg], capture_output=True, text=True)
                    sections.append(("Commit del dispatcher", c.returncode == 0, c.stdout + c.stderr or msg))
                if ok and runner == "claude" and question(out):
                    asked = sid  # the resumed session keeps working on the same worktree
                    break
                if not ok:
                    break
                rok, rout, _, so = ask(f"{ctx}\n{task_txt}\nOtra IA implementó esta tarea en la rama {branch} (worktree actual). Revisa con `git diff {review_base}...HEAD` y `git log {review_base}..HEAD` (base de la tarea, no necesariamente main): "
                                "¿cumple lo pedido?, ¿bugs, casos límite, seguridad o cambios fuera de alcance?, ¿faltan tests? NO modifiques nada. "
                                "Responde con el veredicto y los hallazgos concretos (archivo y línea) en el esquema pedido.",
                                      "plan", MODELS["review"], "review", wt, agents=PLAN_AGENTS, schema=REVIEW_SCHEMA)
                if so:
                    verdict_ok, rout = rok and so["veredicto"] == "APROBADO", fmt_review(so)
                else:  # no structured output: fall back to the text
                    verdict_ok = rok and "VEREDICTO: APROBADO" in rout
                if (not rok) and is_claude_limit(rout):
                    mark_claude_limited(rout)
                    sections.append((f"Revisión · {MODELS['review']}{tag}", False,
                                     (rout or "") + "\n(Revisión aplazada por límite Claude; worktree conservado. Reencolar tras el reset.)"))
                    review_deferred = True
                    break
                sections.append((f"Revisión · {MODELS['review']}{tag}", rok, rout))
                if verdict_ok or not rok:
                    break
                feedback = f"Una revisión previa de tu trabajo pidió cambios. Corrígelos ahora sobre lo ya hecho (no empieces de cero):\n{rout[-3000:]}\n\n"
    except Exception as e:
        sections.append(("Error", False, str(e)))
        ok = False
    body = "".join(f"\n\n--- {h} ({stamp()}){'' if k else ' — FALLÓ'} ---\n{o[-6000:]}" for h, k, o in sections)
    if mode == "exec":
        body += f"\nRama: {branch} · worktree: {wt}"
    if asked:
        body += (f"\n\nSesión IA: {asked} mode={mode} runner={runner}\n"
                 "Para responder: escribe «RESPUESTA» seguido de dos puntos y tu texto debajo de esta línea, y pon la etiqueta ia·continuar.")
    if review_deferred and ok and not asked:
        toggle(api, tags, t, "ia·trabajando", False)
        api("PATCH", "/module/tasks", {"id": tid, "note": f"{note}{body}".strip(), "statusId": st["Review"]})
        log(f"end t{tid} ok={ok} review_deferred runner={runner} model={who.split(' · ')[-1].replace(' ', '_')} secs={int(time.time() - t_run)}")
        subprocess.run([sys.executable, str(HERE / "notify.py"), ttl, f"Hecho en {branch}; revisión Opus aplazada por el límite de 5h. Reencola tras el reset.", "3", "--task", str(tid)])
        return
    done_tag = "ia·fallo" if not ok else ("ia·pregunta" if asked else "ia·plan-listo" if mode == "plan" else ("ia·hecho" if verdict_ok else "ia·cambios"))
    toggle(api, tags, t, "ia·trabajando", False)
    toggle(api, tags, t, done_tag, True)
    api("PATCH", "/module/tasks", {"id": tid, "note": f"{note}{body}".strip(), "statusId": st["Review"]})
    log(f"end t{tid} ok={ok} {done_tag} runner={runner} model={who.split(' · ')[-1].replace(' ', '_')} secs={int(time.time() - t_run)}")
    msg = {"ia·fallo": "Revisa la nota de la tarea", "ia·plan-listo": "Plan listo para revisar", "ia·hecho": "Implementada y aprobada por la revisión",
           "ia·cambios": "Implementada, la revisión pide cambios", "ia·pregunta": "La IA tiene una pregunta"}[done_tag]
    detail = notify_detail(done_tag, sections)
    extra = f"\nRama {branch}" if mode == "exec" and not in_place else ""
    subprocess.run([sys.executable, str(HERE / "notify.py"), ttl, f"{msg}.{(' ' + detail) if detail else ''}{extra}", "4" if done_tag in ("ia·fallo", "ia·cambios") else "3", "--task", str(tid)])
    if tags.get(MAIL) in set(t.get("tags") or []):
        mail_result(api, tags, t, msg, body, done_tag)


def mail_result(api, tags, t, msg, body, done_tag):
    """Email the outcome of one run (3 tries, the network can blip); the tag is removed only when the mail went out."""
    text = f"{t['description']}\n{msg} ({done_tag})\n{body[-8000:]}"
    cmd = [str(Path.home() / ".local/bin/send-mail"), "--to", MAIL_TO, "--subject", f"IA: {t['description'][:70]} — {msg}", "--text", text]
    env = {**os.environ, "PATH": f"{HIMALAYA_DIR}:{os.environ.get('PATH', '')}"}
    for attempt, wait in enumerate((0, 30, 90), 1):
        time.sleep(wait)
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        log(f"mail t{t['id']} try {attempt} rc={p.returncode} {(p.stdout + p.stderr).strip()[-200:]}")
        if p.returncode == 0:
            toggle(api, tags, t, MAIL, False)
            return


if __name__ == "__main__":
    main()
