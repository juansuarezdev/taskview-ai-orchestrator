#!/usr/bin/env python3
"""Run TaskView tasks tagged for an AI agent.

A task (status Backlog/TODO) runs when it is assigned to an AI user AND carries a mode tag.
The assignee decides WHO (ia-users.json): Claude -> claude, OpenCode -> OpenCode (free model, Opus still reviews); both -> Claude.
The tag decides WHAT:
  ia·plan      -> plans only (read-only), writes the plan into the task note, moves it to Review
  ia·ejecutar  -> implements it in its own git worktree/branch `ai/t<id>`, moves it to Review; for Board B, Board V (goals 29/30) and Project Board N (repo repo-n), then merges `ai/t<id>` into main and pushes origin if the remote exists (never --force)
A mode tag with no AI assignee does nothing. Tasks assigned to the user (owner in ia-users.json) are never touched.
If Claude hits the 5-hour session limit during exec (or a cooldown file says so), the same task continues with OpenCode automatically; Opus review is still deferred on limit.
If OpenCode fails for quota/rate limit/credits it retries once on the reserve (.opencode-fallback-model = Magpie group/siempre;
sensitive boards in .opencode-fallback-exclude use .opencode-fallback-model-private = Go only); if that fails too: ia·fallo + 'todo agotado' alert.
Models: plan = Opus, exec = Sonnet, then an automatic review by Opus (ia·hecho if approved, ia·cambios if not).
Plan/exec sessions delegate to subagents by model: `explorer` (Haiku, read-only search) and `tester` (Sonnet, runs tests).
The review answers with a JSON schema (verdict + findings) instead of free text.
Questions: a claude session that lacks information ends with `PREGUNTA: ...`; the task goes to Review with ia·pregunta and the
session id in the note. Answer by writing `RESPUESTA: ...` below the `Sesión IA:` line and tag it ia·continuar: the session is resumed.
Progress tags: ia·trabajando while running, then ia·plan-listo / ia·hecho / ia·cambios / ia·fallo / ia·pregunta.
Packs (ia-packs.json): ia·pack·analizar / implementar / barato / silencioso = mode + email in one tag
(barato is plain ia·ejecutar + mail: the cheap runner comes from assigning OpenCode).
Tag ia·correo on a task: when its run ends the result is emailed (send-mail) and the tag is removed.
One task at a time (flock). Run every minute from launchd. `--dry-run` only lists. `--integrate-main <repo> <branch>` retries a 29/30 merge into main (same path as after ia·hecho; never --force).
"""
import base64, datetime, fcntl, html, json, os, re, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

import ia_users
import orq_config

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"
CODE = Path.home() / "Projects/code"
EXTRA_ROOTS = {"repo-r": Path.home() / "Projects/repo-r",  # boards whose folder is not ~/Projects/code/<repo>; no git repo there = plan only
               "inbox": CODE,  # loose tasks: read-only analysis across every repo
               "repo-h": Path.home() / "vault/projects/project"}
WORKTREES = CODE / ".ai-worktrees"
# Per-board default worktree base while nothing is merged to main (the owner, 15 nov): new ai/t<id> branches start from the
# board scaffold instead of main. A "WORKTREE_BASE: <branch>" line in the task note still wins (chain phases that way).
BOARD_BASE = {"repo-b": "ai/t382", "repo-v": "ai/t394"}
# the owner, 15 nov 2026 20:42 : finished work in projects 29/30 lands on main and origin.
MERGE_TO_MAIN_REPOS = {"repo-b", "repo-v", "repo-n", "repo-s"}  # repo-n: Project Board N (directiva autónoma de the owner, 15 nov 21:28)
# Test command run on main before pushing (default `npm test`). repo-n is Swift: scripts/test.sh = swift test of AppCore (+ xcodebuild if present).
MAIN_TEST_CMD = {"repo-n": ["bash", "scripts/test.sh"]}
# repo-s (the owner, 21 nov 2026): Laravel in app/, no root package.json. Pest via the sail-8.5/app image, as the
# SP-498/SP-499 reviews did. The tests run on a `git archive HEAD:app` copy in /private/tmp (vendor/ and .env are
# gitignored, and a failed run must not leave untracked files in the main checkout): composer install (persistent
# cache) + php artisan test with a throwaway APP_KEY. Nothing is written to the principal checkout.
LARAVEL_DOCKER_TEST = {"repo-s": "app"}
COMPOSER_CACHE = Path.home() / ".cache/ai-dispatch-composer"


def main_test_cmd(repo_dir):
    """Test command for main before pushing, or None when the repo has nothing to run (no root package.json)."""
    name = repo_dir.name
    if name in MAIN_TEST_CMD:
        return MAIN_TEST_CMD[name]
    sub = LARAVEL_DOCKER_TEST.get(name)
    if sub is not None and (repo_dir / sub / "artisan").exists():
        import base64, secrets, tempfile
        tmp = Path(tempfile.mkdtemp(prefix=f"ai-main-test-{name}-", dir="/private/tmp"))
        arch = subprocess.run(f"git -C '{repo_dir}' archive HEAD {sub} | tar -x -C '{tmp}'", shell=True, capture_output=True, text=True, timeout=120)
        if arch.returncode != 0:
            return ["sh", "-c", f"echo 'git archive falló: {arch.stderr.strip()[:200]}' >&2; exit 1"]
        COMPOSER_CACHE.mkdir(parents=True, exist_ok=True)
        uid = f"{os.getuid()}:{os.getgid()}"
        key = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
        return ["docker", "run", "--rm", "--user", uid, "-e", "HOME=/tmp", "-e", "COMPOSER_HOME=/tmp/c",
                "-e", "COMPOSER_CACHE_DIR=/composer-cache", "-e", f"APP_KEY={key}",
                "-v", f"{COMPOSER_CACHE}:/composer-cache", "-v", f"{tmp / sub}:/app", "-w", "/app",
                "--entrypoint", "sh", "sail-8.5/app:latest", "-c",
                "{ [ -f .env ] || cp .env.example .env; } && composer install --no-interaction --no-progress --prefer-dist -q && php artisan test --colors=never"]
    if (repo_dir / "package.json").exists():
        return ["npm", "test"]
    return None
NPM_TEST_TIMEOUT = 15 * 60  # repo-v e2e included


def note_overrides(note):
    """Optional per-card lines in the note: "MODEL: <claude model id>", "BUDGET_USD: <n>", "TIMEOUT_MIN: <n>".
    Lets one card run on another model (e.g. claude-fable-5-2 for a long design/audit pass) without
    changing the board-wide MODELS. Applies to Claude exec runs only."""
    g = lambda k: (re.search(rf"(?im)^{k}:\s*(\S+)", note or "") or [None, None])[1]
    t = g("TIMEOUT_MIN")
    return g("MODEL"), g("BUDGET_USD"), (int(t) * 60 if t and t.isdigit() else None)


def worktree_base(repo, note):
    m = re.search(r"(?im)^WORKTREE_BASE:\s*(\S+)", note or "")
    return m.group(1) if m else BOARD_BASE.get(repo)
LOG = HERE / "logs/ai-dispatch.log"
REPOS = {"Board M": "repo-m", "Board G": "repo-g", "Board E": "repo-e", "TaskView": "taskview-community", "Board R": "repo-r", "Board GC": "repo-gc", "Board SB": "repo-sb", "local-model": "local-model",
         "Board A": "repo-a", "Board H": "repo-h", "Inbox": "inbox", "Board C": "repo-c", "Board B": "repo-b", "Board V": "repo-v",
         "Project N": "repo-n", "Board S": "repo-s"}
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
MODELS = {"plan": "claude-opus-5-5", "exec": "claude-sonnet-5-5", "review": "claude-sonnet-5-5",
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
                 "swift build", "swift test", "swift package", "xcodebuild", "xcodegen", "bash scripts/test.sh", "scripts/test.sh", "xcrun simctl list",
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
# the owner, 20 nov 2026 23:01 : Board S runs everything in Docker (Laravel/Sail, Playwright captures), so its
# sessions may use docker; scoped to that repo only (its checkout and its .ai-worktrees/repo-s-t<N> worktrees).
REPO_EXTRA_BASH = {"repo-s": ["docker run", "docker compose", "docker ps", "docker images", "docker logs", "docker exec",
                                     "docker build", "docker pull", "docker image inspect"]}


# the owner, 21 nov 2026 11:57 , t456: Board V sessions get full Bash inside the repo-v checkout and its
# .ai-worktrees/repo-v-t<N> worktrees (git rm/checkout/commit, npm run check, npm test, APP_DB=... prefixes). The global
# DENIED list still applies (rm, git push/reset/merge/rebase, curl, sudo) plus no Docker for this repo.
REPO_BASH_ALL = {"repo-v"}
REPO_EXTRA_DENIED = {"repo-v": ["docker", "docker-compose", "podman", "colima"]}


def _repo_of(cwd):
    name = Path(str(cwd)).name
    for repo in {*REPO_EXTRA_BASH, *REPO_BASH_ALL, *REPO_EXTRA_DENIED}:
        if name == repo or name.startswith(repo + "-t"):
            return repo
    return None


def repo_extra_allowed(cwd):
    repo = _repo_of(cwd)
    if repo in REPO_BASH_ALL:
        return ["Bash"]
    if repo in REPO_EXTRA_BASH:
        return _bash_rules(REPO_EXTRA_BASH[repo])
    return []


def repo_extra_denied(cwd):
    return _bash_rules(REPO_EXTRA_DENIED.get(_repo_of(cwd), []))


# the owner, 20 nov 2026 19:01/23:01: these goal ids are served first on every tick (before the round-robin of the rest).
PRIORITY_PROJECTS = [32]  # 32 = Board S
WEB = ["WebSearch", "WebFetch"]  # analysis (plan) sessions may research the web
WEB_TASKS_FILE = Path(__file__).resolve().parent / "web-tasks.txt"  # task ids (one per line) whose exec sessions may also use the web, granted by the owner


def web_ok(perm):
    if perm == "plan":
        return True
    try:
        return str(CURRENT_TASK).strip() in {l.split("#")[0].strip() for l in WEB_TASKS_FILE.read_text().splitlines()}
    except OSError:
        return False
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
    Assigned to the user -> never. Needs a mode tag (packs first) AND an AI assignee, which picks the runner
    (both AIs: CFG runner_both; nobody assigned: CFG default_runner, None by default = nothing runs)."""
    if owner and owner in assignees:
        return None
    trig = next((n for n in TRIGGERS if n in tag_names), None)
    ais = [r for r, e in ia_users.RUNNERS.items() if e in assignees]
    runner = (CFG["runner_both"] if CFG["runner_both"] in ais else ais[0]) if ais else (CFG["default_runner"] if not assignees else None)
    if not trig or not runner:
        return None
    return trig, TRIGGERS[trig], runner


LAST_GOAL_FILE = HERE / "logs/.last-goal"  # board of the last card started (round-robin across boards)
PAUSE_FILE = HERE / "logs/.pausa-orquestador"  # while it exists no new card starts (set from the panel, orq-panel.py)


def mark_goal(gid):
    try:
        LAST_GOAL_FILE.write_text(str(gid))
    except OSError:
        pass


def main():
    dry = "--dry-run" in sys.argv
    if "--usage" in sys.argv:  # read-only report; `--usage --probe` refreshes the figures first (one tiny Haiku call)
        if "--probe" in sys.argv:
            probe_usage()
        print(usage_report())
        return
    integrate_args = None
    if "--integrate-main" in sys.argv:
        argv = sys.argv[1:]
        i = argv.index("--integrate-main")
        rest = [a for a in argv[i + 1:] if not a.startswith("--")]
        if len(rest) < 2:
            print("uso: ai-dispatch.py --integrate-main <repo> <rama>", file=sys.stderr)
            sys.exit(2)
        integrate_args = (rest[0], rest[1])
    lock = open(HERE / "logs/.ai-dispatch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if integrate_args:
            print("ai-dispatch ocupado (lock); no se reintenta el merge ahora", file=sys.stderr)
            sys.exit(1)
        return
    if integrate_args:
        repo, branch = integrate_args
        if repo not in MERGE_TO_MAIN_REPOS:
            print(f"repo {repo} no está en la lista de merge a main ({sorted(MERGE_TO_MAIN_REPOS)})", file=sys.stderr)
            sys.exit(2)
        root = EXTRA_ROOTS.get(repo, CODE / repo)
        ok, detail, _ask = integrate_to_main(root, branch)
        print(detail)
        sys.exit(0 if ok else 1)
    if CFG_ERRORS:  # printed in --dry-run; logged once per distinct error, not every tick
        msg = f"orq-config.json: {CFG_ERRORS}; se usan los valores por defecto en esas claves"
        mark = HERE / "logs/.orq-config-error"
        if dry:
            print(msg)
        elif not mark.exists() or mark.read_text() != msg:
            mark.write_text(msg)
            log(msg)
    if not dry and CFG["tick_secs"] > 60:  # launchd runs every 60 s; a longer interval skips ticks
        try:
            if time.time() - TICK_FILE.stat().st_mtime < CFG["tick_secs"] - 5:
                return
        except OSError:
            pass
        TICK_FILE.touch()
    if PAUSE_FILE.exists():  # manual pause (orq-panel.py): start nothing new; a run in progress is never touched
        if dry:
            print(f"PAUSADO desde {time.strftime('%F %T', time.localtime(PAUSE_FILE.stat().st_mtime))}: no se arrancaría nada")
        return
    # Pausa: Claude en cooldown y OpenCode/reserva agotados (opencode_exhausted) -> no arrancar nada (evita quemar tarjetas a ia·fallo)
    if not dry:
        paid_release_check()
    if not dry and claude_is_limited() and ALERT_FILE.exists() and not _read_line(_OC_FREE_FILE):
        if opencode_exhausted():
            mark = HERE / "logs/.pausa-agotado"
            until = CLAUDE_LIMIT_FILE.read_text().strip()
            if not mark.exists() or mark.read_text().strip() != until:
                mark.write_text(until)
                log(f"pausa: Claude en cooldown y OpenCode agotado; no se arranca nada hasta {time.strftime('%H:%M', time.localtime(float(until)))}")
            return
    if not dry:
        usage_alerts(usage_now())
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = token(c)
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    owner = ia_users.owner_email(c)
    if not owner:  # without it the user's own tasks could be picked up
        log("no owner email (ia-users.json owner / .credentials email): nothing runs")
        return

    # Fair share: start after the board that ran last, so one board with a long queue doesn't take every turn
    goals = api("GET", "/module/goals")
    try:
        last = int(LAST_GOAL_FILE.read_text().strip())
        i = next(k for k, x in enumerate(goals) if x["id"] == last)
        goals = goals[i + 1:] + goals[:i + 1]
    except (OSError, ValueError, StopIteration):
        pass
    first = priority_goals(api)
    goals = sorted(goals, key=lambda x: 0 if x["id"] in first else 1 if x["id"] in PRIORITY_PROJECTS else 2)  # stable: priority cards' boards, priority boards, then the rotation
    for g in goals:
        repo = REPOS.get(g["name"])
        if not repo or g.get("archive"):
            continue
        if g["name"] in CFG["disabled_boards"]:
            if dry:
                print(f"[{g['name']}] desactivado en orq-config.json")
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
        if not {"In Progress", "Review"} <= set(st):  # run() moves cards there; without the columns it would crash after the work
            if dry:
                print(f"[{g['name']}] sin columnas In Progress/Review: no se arranca nada")
            continue
        collab = ia_users.collaborators(api, gid)
        names = {v: k for k, v in tags.items()}
        busy_cache = {}
        for sname in ("TODO", "Backlog", "Review"):
            col = "null" if sname == "Backlog" else st.get(sname)  # Backlog is not a status row: tasks with no status, addressed as `null`
            if col is None:
                continue
            cur = "null"
            while cur:
                r = api("GET", f"/module/kanban/tasks/{gid}/{col}/{cur}")
                prio = {tid: i for i, tid in enumerate(CFG["priority_tasks"])}
                for t in sorted(r["tasks"], key=lambda x: prio.get(x["id"], len(prio))):  # stable: priority cards lead their page
                    have = set(t.get("tags") or [])
                    who = ia_users.assignee_emails(t, collab)
                    if owner in who or t.get("complete"):  # the user's own task; done tasks may keep a stale trigger tag
                        continue
                    # Review: answered questions (ia·continuar) resume; a mode tag put back on a Review task
                    # (run() removes it on start, so it was re-queued on purpose, e.g. after ia·fallo) re-runs it below
                    if sname == "Review" and tags.get(CONTINUE) in have:
                        res, err = parse_resume(t.get("note") or "")
                        if dry:
                            print(f"[{g['name']}] t{t['id']} continuar: {err or res['mode'] + '/' + res['runner']}")
                        elif err:
                            toggle(api, tags, t, CONTINUE, False)
                            log(f"continue t{t['id']} skipped: {err}")
                            subprocess.run([sys.executable, str(HERE / "notify.py"), f"IA: {t['description'][:60]}", f"No puedo continuar: {err}", "3", "--task", str(t["id"])])
                        elif res["runner"] == "claude" and (claude_paced() or claude_reserved(g["name"])):
                            pass  # held: a Claude session cannot resume elsewhere; it continues when the window allows
                        elif res["runner"] == "opencode" and not opencode_usable(g["name"]):
                            pass  # held until Go is back (sensitive board, no free model)
                        else:
                            mark_goal(gid)
                            run(api, g, repo, st, tags, t, res["mode"], CONTINUE, res["runner"], res)
                            return
                        continue
                    hit = select({names[i] for i in have if i in names}, who, owner)
                    if hit:
                        trig, mode, runner = hit
                        wait = base_blocker(api, gid, st, tags, repo, t, busy_cache)
                        if wait:  # chained phase: its base branch is not ready yet, try the next card
                            if dry:
                                print(f"[{g['name']}] t{t['id']} espera: {wait}")
                            continue
                        if dry:
                            print(f"[{g['name']}] t{t['id']} {mode}/{runner} base={worktree_base(repo, t.get('note')) or 'main'}: {t['description']}")
                        else:
                            if runner == "claude" and (claude_paced() or claude_reserved(g["name"]) or (not CFG["handoff_to_opencode"] and claude_is_limited())):
                                if mode != "exec" or not CFG["handoff_to_opencode"] or not opencode_usable(g["name"]):
                                    continue  # held; cards of other runners further down may still start
                                log(f"t{t['id']} pacing → runner=opencode")
                                runner = "opencode"
                            elif runner == "opencode" and not opencode_usable(g["name"]):
                                continue  # Go out and no free model here (sensitive board): wait instead of ia·fallo
                            mark_goal(gid)
                            run(api, g, repo, st, tags, t, mode, trig, runner)
                            return  # one task per tick
                cur = str(r["nextCursor"]) if r.get("nextCursor") else ""


# A card whose base is "ai/t<N>" (BOARD_BASE or the note's WORKTREE_BASE) waits while card N is still pending or
# running, or while that branch does not exist yet, so chained phases run in order whatever the kanban order is.
BASE_BUSY_TAGS = ("ia·plan", "ia·ejecutar", CONTINUE, "ia·trabajando", "ia·pregunta", "ia·fallo")


def base_blocker(api, gid, st, tags, repo, t, cache):
    base = worktree_base(repo, t.get("note"))
    root = EXTRA_ROOTS.get(repo, CODE / repo)
    if not base or not (root / ".git").exists() or (WORKTREES / f"{repo}-t{t['id']}").exists():
        return None  # no base pin, not a git board, or the worktree already exists (resume/re-run)
    if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", base], capture_output=True).returncode != 0:
        return f"la rama base {base} aún no existe"
    m = re.fullmatch(r"ai/t(\d+)", base)
    if not m:
        return None
    if "busy" not in cache:
        ids = {tags[n] for n in BASE_BUSY_TAGS if n in tags}
        busy = set()
        for sname in ("TODO", "Backlog", "In Progress", "Review"):
            col = "null" if sname == "Backlog" else st.get(sname)
            cur = "null" if col is not None else ""
            while cur:
                r = api("GET", f"/module/kanban/tasks/{gid}/{col}/{cur}")
                busy |= {x["id"] for x in r["tasks"] if not x.get("complete") and set(x.get("tags") or []) & ids}
                cur = str(r["nextCursor"]) if r.get("nextCursor") else ""
        cache["busy"] = busy
    if int(m.group(1)) in cache["busy"] and int(m.group(1)) != t["id"]:
        return f"t{m.group(1)} ({base}) sigue pendiente"
    return None


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
CURRENT_GOAL = ""  # board name of the task being run (sensitive boards never use the public reserve)
OC_LAST = ""  # model the last OpenCode run actually used (primary or reserve)
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


def ask(prompt, perm, model, mode, cwd, agents=None, schema=None, resume=None, budget=None, timeout=None):
    """Run one claude -p session; returns (ok, text, session_id, structured_output)."""
    extra = [*(["--agents", json.dumps(agents)] if agents else []), *(["--json-schema", json.dumps(schema)] if schema else []),
             *(["--resume", resume] if resume else [])]
    try:
        p = subprocess.run(["claude", "-p", prompt, "--model", model, "--permission-mode", perm, "--max-budget-usd", budget or BUDGET[mode],
                            "--output-format", "stream-json", "--verbose", *extra,
                            "--allowedTools", *ALLOWED, *repo_extra_allowed(cwd), *(WEB if web_ok(perm) else []), "--disallowedTools", *DENIED, *repo_extra_denied(cwd)],
                           cwd=cwd, env={**os.environ, "PATH": f"{Path.home()}/.local/bin:{HERD_BIN}:{HIMALAYA_DIR}:{os.environ.get('PATH', '')}", "TASKVIEW_NOHOOK": "1", "TASKVIEW_TASK_ID": CURRENT_TASK, "AI_DISPATCH": "1", **RUN_ENV}, capture_output=True, text=True, timeout=timeout or TIMEOUT[mode])
    except subprocess.TimeoutExpired as e:
        record_usage(e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout)
        return False, f"Timeout ({(timeout or TIMEOUT[mode]) // 60} min).", None, None
    except Exception as e:
        return False, f"Error: {e}", None, None
    snap = record_usage(p.stdout)
    o, plain = None, []
    for line in p.stdout.splitlines():  # stream-json: one event per line, the last `result` is what --output-format json printed
        try:
            j = json.loads(line)
        except ValueError:
            plain.append(line)
            continue
        if isinstance(j, dict) and j.get("type") == "result":
            o = j
    if o is None:  # no result event (crash before output): keep whatever plain text there is
        return False, ("\n".join(plain) or p.stderr or "").strip(), None, None
    if snap:  # one line per run: what it cost and where the windows are, to calibrate BUDGET against the 5h window
        log(f"usage t{CURRENT_TASK} {mode} {model} cost=${float(o.get('total_cost_usd') or 0):.2f} 5h={snap['five_pct']:.0f}% 7d={'%.0f' % snap['week_pct'] if snap.get('week_pct') is not None else '?'}%")
    return p.returncode == 0 and not o.get("is_error"), (o.get("result") or "").strip(), o.get("session_id"), o.get("structured_output")


OC_BIN = str(Path.home() / ".opencode/bin/opencode")  # launchd PATH does not include it
_OC_FILE = HERE / ".opencode-model"  # one line, e.g. opencode/nemotron-3-ultra-free; overrides the default below
OC_MODEL = _OC_FILE.read_text().strip() if _OC_FILE.exists() else "opencode/deepseek-v4.1-flash-free"  # free model; no paid OpenCode plan
# Fallback chain (2026-11-15, "IA siempre trabajando"): Claude -> OpenCode (OC_MODEL, OpenCode Go) -> Magpie routing group.
# When an OpenCode run fails for quota / rate limit / credits, it is retried ONCE with the reserve model instead of ia·fallo.
# The reserve is a Magpie group (gateway 127.0.0.1:3425, provider `magpie-gw` in ~/.config/opencode/opencode.json):
#   group/siempre          OpenCode Go models first, then NVIDIA build + Gemini free endpoints once their keys are in Magpie
#   group/siempre-privado  OpenCode Go only, for boards with sensitive data (free endpoints may train on what they get)
_OC_FALLBACK_FILE = HERE / ".opencode-fallback-model"  # one line; empty/absent = no reserve retry
_OC_FALLBACK_PRIVATE_FILE = HERE / ".opencode-fallback-model-private"  # reserve for sensitive boards; empty/absent = no retry there
_OC_SENSITIVE_FILE = HERE / ".opencode-fallback-exclude"  # board names (fnmatch, case-insensitive, # comments) that never use the public reserve
_DEFAULT_SENSITIVE = ["SiteApp*", "Board M", "Board V"]
ALERT_FILE = HERE / "logs/.ia-agotado"  # json of the last "everything exhausted" event
ALERT_EVERY = 30 * 60  # at most one phone/Mac alert per 30 min


def _read_line(f):
    try:
        return next((l.strip() for l in f.read_text().splitlines() if l.strip() and not l.strip().startswith("#")), "")
    except Exception:
        return ""


def _sensitive_patterns():
    try:
        pats = [l.strip() for l in _OC_SENSITIVE_FILE.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
    except Exception:
        pats = []
    return pats or _DEFAULT_SENSITIVE


def is_sensitive_board(board):
    """Unknown board = sensitive (safe default)."""
    import fnmatch
    if not board:
        return True
    return any(fnmatch.fnmatch(board.lower(), p.lower()) for p in _sensitive_patterns())


def fallback_model_for(board):
    return _read_line(_OC_FALLBACK_PRIVATE_FILE if is_sensitive_board(board) else _OC_FALLBACK_FILE)


# Last resort (2026-11-17, the owner): free OpenCode Zen models only once the paid ones (Claude, OpenCode Go) are out, and
# never on sensitive boards (free endpoints may train on what they get). One alert when going free, one when Go is back.
_OC_FREE_FILE = HERE / ".opencode-free-model"  # one line, e.g. opencode/deepseek-v4.1-flash-free; empty/absent = never free
FREE_STATE = HERE / "logs/.modo-gratis.json"  # {"since", "model"} while the free model stands in for Go


def free_model_for(board):
    return "" if is_sensitive_board(board) else _read_line(_OC_FREE_FILE)


def opencode_usable(board, now=None):
    """An OpenCode run on this board can do something now: Go has quota, or the board may use the free model."""
    return not opencode_exhausted(now) or bool(free_model_for(board))


def notify(title, body, prio="3"):
    subprocess.run([sys.executable, str(HERE / "notify.py"), title, body, prio], check=False)


def go_back_at():
    """Readable time when Go is expected back (from Magpie's date), or ''."""
    try:
        err = str(json.loads(ALERT_FILE.read_text()).get("error", ""))
        m = re.search(r"not asked again before (\w{3} \d{1,2} \d{2}:\d{2}) UTC\+0", err)
        if m:
            when = datetime.datetime.strptime(f"{time.gmtime().tm_year} {m.group(1)}", "%Y %b %d %H:%M").replace(tzinfo=datetime.timezone.utc)
            return time.strftime("%d/%m %H:%M", time.localtime(when.timestamp()))
    except Exception:
        pass
    return ""


def enter_free_mode(model, now=None):
    """First run on the free model since Go ran out: record it and tell the owner once."""
    if FREE_STATE.exists():
        return
    FREE_STATE.parent.mkdir(exist_ok=True)
    FREE_STATE.write_text(json.dumps({"since": now or time.time(), "model": model}))
    back = go_back_at()
    log(f"modo gratis: {model} en lugar de OpenCode Go{' hasta ' + back if back else ''}")
    notify("IA en modo gratis", f"OpenCode Go sin cuota{' hasta ' + back if back else ''}: las tarjetas de OpenCode usan {model} "
           "(tableros sensibles esperan). Te aviso cuando vuelva Go.")


def paid_release_check(now=None):
    """Once Go has quota again (its date passed), leave free mode and tell the owner."""
    if not FREE_STATE.exists() or opencode_exhausted(now):
        return
    FREE_STATE.unlink(missing_ok=True)
    log("modo gratis: OpenCode Go vuelve a tener cuota")
    notify("OpenCode Go disponible", "Ya hay cuota de OpenCode Go: las tarjetas vuelven al modelo de pago. Reencola las que quedaron en ia·fallo.", "4")


QUOTA_RE = re.compile(r"\b429\b|\b402\b|too many requests|rate[ _-]?limit|\bcredits?\b|quota|insufficient[ _](?:balance|funds|credit)|"
                      r"payment required|usage limit|limit (?:reached|exceeded)|resource[ _]exhausted|out of (?:credit|tokens)|billing", re.I)


def is_quota_error(ok, diag):
    """Only failed runs count (a successful run may well talk about credits); a timeout is never quota."""
    if ok or not diag or diag.lstrip().startswith("Timeout ("):
        return False
    return bool(QUOTA_RE.search(diag))
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


# --- Usage pacing: make the 5h window last at least PACE_HOURS instead of reacting only once the limit is hit ---
# Figures come from the `rate_limit_event` lines of `claude -p --output-format stream-json` (five_hour / seven_day
# utilization + reset time): ask() saves them after every run and a tiny Haiku probe refreshes them when they are
# stale. Newer samples from the interactive statusline (statusline-burn.tsv, same window) raise the figure.
USAGE_FILE = HERE / "logs/usage-snapshot.json"
BURN_TSV = Path.home() / ".claude/state/statusline-burn.tsv"  # "<unix ts>\t<5h %>" lines, read-only here
PACE_HOURS = 4  # the window must last at least this long
PACE_SLACK = 10  # % allowed on top of the straight line
PACE_HARD = 90  # % from which no Claude run starts, whatever the line says
USAGE_STALE = 15 * 60  # older figures are only a lower bound
USAGE_ALERTS = (70, 90)  # one phone alert per window at each level
PROBE_MODEL = "claude-haiku-4-5"
# The weekly window (seven_day) is shared with the owner's own sessions: same straight line over 7 days, so the dispatcher never
# spends the week ahead of time. A wider slack than the 5h line because the weekly figure moves slowly.
WEEK_HOURS = 168
WEEK_SLACK = 15
WEEK_HARD = 90
# Usage reservations set by the owner: windows (local time) in which Claude only runs for the boards in "solo" (empty = for none),
# so the 5h window reaches the reserved work untouched. [{"desde": "2026-11-19 00:00", "hasta": "2026-11-19 15:00", "solo": []}]
RESERVE_FILE = HERE / ".reserva-claude.json"


def claude_reserved(board=None, now=None):
    """Reason string when a reservation keeps Claude away from this board now, else ""."""
    now = now or time.time()
    try:
        windows = json.loads(RESERVE_FILE.read_text())
    except Exception:
        return ""
    for w in windows if isinstance(windows, list) else []:
        try:
            a, b = (time.mktime(time.strptime(w[k], "%Y-%m-%d %H:%M")) for k in ("desde", "hasta"))
        except (KeyError, TypeError, ValueError):
            continue
        if a <= now < b and board not in (w.get("solo") or []):
            return w.get("motivo") or f"reserva hasta {w['hasta']}"
    return ""


def parse_usage(stdout):
    """Latest usage snapshot in a stream-json output, or None."""
    snap = None
    for line in (stdout or "").splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            info = json.loads(line).get("rate_limit_info") or {}
            win = info.get("unifiedWindows") or {}
            five, week = win.get("five_hour") or {}, win.get("seven_day") or {}
            if not five and info.get("rateLimitType") == "five_hour":
                five = info
            if five.get("utilization") is None or not five.get("resetsAt"):
                continue
            snap = {"ts": time.time(), "five_pct": round(float(five["utilization"]) * 100, 1), "five_resets": float(five["resetsAt"]),
                    "week_pct": round(float(week["utilization"]) * 100, 1) if week.get("utilization") is not None else None,
                    "week_resets": week.get("resetsAt"), "source": "claude -p"}
        except Exception:
            continue
    return snap


def record_usage(stdout, source=None):
    """Save the snapshot found in a claude run's output (if any) and fire the level alerts; returns it."""
    snap = parse_usage(stdout)
    if not snap:
        return None
    if source:
        snap["source"] = source
    try:
        USAGE_FILE.parent.mkdir(exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, USAGE_FILE)
        usage_alerts(snap)
    except Exception:
        pass
    return snap


def usage_now(now=None):
    """Saved snapshot of the window in force, raised by newer statusline samples; None when nothing is known about it."""
    now = now or time.time()
    try:
        s = json.loads(USAGE_FILE.read_text())
        s["ts"], s["five_pct"], s["five_resets"] = float(s["ts"]), float(s["five_pct"]), float(s["five_resets"])
    except Exception:
        return None
    if now >= s["five_resets"]:  # that window is over
        return None
    try:
        taken = s["ts"]
        for line in BURN_TSV.read_text().splitlines()[-200:]:
            ts, pct = (float(x) for x in line.split()[:2])
            if taken < ts <= now:  # usage only grows inside a window, so the highest newer sample wins
                s["ts"] = max(s["ts"], ts)
                if pct > s["five_pct"]:
                    s["five_pct"], s["source"] = pct, "statusline"
    except Exception:
        pass
    return s


def week_pace(s, now=None):
    """(hold, used %, allowed %) for the weekly window, or None when the snapshot has no live weekly figure."""
    now = now or time.time()
    try:
        used, resets = float(s["week_pct"]), float(s["week_resets"])
    except (KeyError, TypeError, ValueError):
        return None
    if now >= resets:
        return None
    elapsed_h = max(0.0, WEEK_HOURS - (resets - now) / 3600)
    allowed = min(100.0, elapsed_h / WEEK_HOURS * 100 + WEEK_SLACK)
    return used > allowed or used >= WEEK_HARD, used, allowed


def pace(s, now=None):
    """(hold, used %, minutes into the window, allowed %) of the 5h line: allowed = min(100, elapsed/240*100 + slack); hold above it
    or from PACE_HARD. The weekly line (week_pace) also holds; the figures returned stay the 5h ones."""
    now = now or time.time()
    elapsed = max(0.0, 300 - (s["five_resets"] - now) / 60)
    allowed = min(100.0, elapsed / (PACE_HOURS * 60) * 100 + PACE_SLACK)
    w = week_pace(s, now)
    hold = s["five_pct"] > allowed or s["five_pct"] >= PACE_HARD or bool(w and w[0])
    return hold, s["five_pct"], elapsed, allowed


def probe_usage():
    """One minimal Haiku call just to read the rate-limit figures; returns the snapshot or None."""
    try:
        p = subprocess.run(["claude", "-p", "Reply with: ok", "--model", PROBE_MODEL, "--max-budget-usd", "0.3", "--output-format", "stream-json",
                            "--verbose", "--allowedTools", "Read"], cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PATH": f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}", "TASKVIEW_NOHOOK": "1", "AI_DISPATCH": "1"})
    except Exception:
        return None
    return record_usage(p.stdout, "probe")


_PACING = []  # this tick's decision, taken once


def pacing():
    """Decision before starting a Claude run: None = no usage data (behave as before), else (hold, used, elapsed, allowed)."""
    if not _PACING:
        s = usage_now()
        # stale or missing figures are refreshed, unless the stale one (a lower bound) already says hold
        if not (s and (time.time() - s["ts"] < USAGE_STALE or pace(s)[0])):
            probe_usage()
            s = usage_now()
        _PACING.append(pace(s) if s else None)
    return _PACING[0]


def claude_paced():
    """True when no Claude run should start now; logs the hold at most once per 10 min."""
    d = pacing()
    if not d or not d[0]:
        return False
    mark = HERE / "logs/.pacing-mark"
    try:
        last = float(mark.read_text())
    except Exception:
        last = 0
    if time.time() - last >= 600:
        mark.write_text(str(time.time()))
        w = week_pace(usage_now() or {})
        week = f"; week {w[1]:.0f}% (allowed {w[2]:.0f}%{', over' if w[0] else ''})" if w else ""
        log(f"pacing: {d[1]:.0f}% used, {d[2]:.0f} min into window, allowed {d[3]:.0f}%{week}, holding")
    return True


def opencode_exhausted(now=None):
    """OpenCode and its reserve are out of quota (same test as the pause in main): until the date Magpie gives
    ("not asked again before Nov 25 02:26 UTC+0"), 24 h after a monthly-limit error, else 6 h after the last event.
    Without this a monthly Go limit burned one queued card to ia·fallo every 6 h."""
    now = now or time.time()
    try:
        a = json.loads(ALERT_FILE.read_text())
        last, err = float(a.get("last_event", 0)), str(a.get("error", ""))
    except Exception:
        return False
    m = re.search(r"not asked again before (\w{3} \d{1,2} \d{2}:\d{2}) UTC\+0", err)
    if m:
        try:
            when = datetime.datetime.strptime(f"{time.gmtime(last).tm_year} {m.group(1)}", "%Y %b %d %H:%M")
            until = when.replace(tzinfo=datetime.timezone.utc).timestamp()
            if until < last:  # the date is in the next year
                until = when.replace(year=when.year + 1, tzinfo=datetime.timezone.utc).timestamp()
            return now < until
        except ValueError:
            pass
    hours = 24 if "monthly" in err.lower() else 6
    return now - last < hours * 3600


def usage_alerts(s):
    """One notification per window at each level of USAGE_ALERTS."""
    if not s:
        return
    f = HERE / "logs/.usage-alerts"
    try:
        st = json.loads(f.read_text())
    except Exception:
        st = {}
    if st.get("resets") != int(s["five_resets"]):
        st = {"resets": int(s["five_resets"]), "sent": []}
    due = [l for l in USAGE_ALERTS if s["five_pct"] >= l and l not in st["sent"]]
    if not due:
        return
    st["sent"] += due
    f.write_text(json.dumps(st))
    reset = time.strftime("%H:%M", time.localtime(s["five_resets"]))
    log(f"usage alert: {s['five_pct']:.0f}% of the 5h window, resets {reset}")
    subprocess.run([sys.executable, str(HERE / "notify.py"), f"Claude 5h al {s['five_pct']:.0f}%",
                    f"Superado el {max(due)}% de la ventana; se renueva a las {reset}. "
                    + ("No arranco más tarjetas con Claude hasta entonces." if max(due) >= PACE_HARD else "El dispatcher frena el ritmo de Claude."),
                    "4" if max(due) >= PACE_HARD else "3"])


def usage_report():
    s = usage_now()
    cool = f"cooldown until {time.strftime('%H:%M', time.localtime(float(CLAUDE_LIMIT_FILE.read_text().strip())))}" if claude_is_limited() else "no cooldown"
    tail = f"claude: {cool} · opencode: {'exhausted' if opencode_exhausted() else 'available'}"
    if not s:
        return f"usage: no data for the current window (dispatcher behaves as before; a probe runs before the next Claude card)\n{tail}"
    hold, used, elapsed, allowed = pace(s)
    week = f"{s['week_pct']:.0f}%" + (f" (resets {time.strftime('%a %H:%M', time.localtime(float(s['week_resets'])))})" if s.get("week_resets") else "") if s.get("week_pct") is not None else "?"
    age = (time.time() - s["ts"]) / 60
    w = week_pace(s)
    why = (f"hard stop from {PACE_HARD}%" if used >= PACE_HARD else f"{used:.0f}% > allowed {allowed:.0f}%" if used > allowed
           else f"week {w[1]:.0f}% > allowed {w[2]:.0f}% (hard stop {WEEK_HARD}%)" if w and w[0] else "?")
    return (f"5h window: {used:.0f}% used, resets {time.strftime('%H:%M', time.localtime(s['five_resets']))} ({elapsed:.0f} min into window)\n"
            f"7d window: {week}\n"
            f"snapshot: {age:.0f} min old, source {s.get('source', '?')}{' (stale: lower bound, re-probed before a start)' if age * 60 >= USAGE_STALE else ''}\n"
            f"pacing: allowed {allowed:.0f}% now (elapsed/{PACE_HOURS * 60}*100 + {PACE_SLACK}, hard stop {PACE_HARD}%)\n"
            f"week pacing: {'allowed ' + format(w[2], '.0f') + '% now' if w else 'no live weekly figure'} (elapsed/{WEEK_HOURS}h*100 + {WEEK_SLACK}, hard stop {WEEK_HARD}%)\n"
            f"decision: {'HOLD Claude (' + why + '); exec cards go to OpenCode if available' if hold else 'OK to start Claude'}\n{tail}")


def opencode_ok_check(ok, out, wt, review_base):
    """Exit 0 with no file changes and no commits is a silent failure for OpenCode."""
    if not ok:
        return ok, out
    dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    ahead = subprocess.run(["git", "-C", str(wt), "rev-list", "--count", f"{review_base}..HEAD"], capture_output=True, text=True).stdout.strip()
    if not dirty and ahead in ("", "0"):
        return False, f"{out}\n(OpenCode terminó sin cambios ni commits: se da por fallo. Reintenta o asigna la tarea a Claude.)"
    return ok, out


def _oc_once(model, prompt, cwd, perm):
    """One `opencode run`; returns (ok, text, diag) where diag = tail of stdout+stderr for error detection."""
    try:
        p = subprocess.run([OC_BIN, "run", "--model", model, prompt], cwd=cwd, capture_output=True, text=True,
                           timeout=OC_TIMEOUT, env={**os.environ, "PATH": f"{Path.home()}/.local/bin:{HERD_BIN}:{HIMALAYA_DIR}:{os.environ.get('PATH', '')}", "TASKVIEW_NOHOOK": "1", "TASKVIEW_TASK_ID": CURRENT_TASK, "AI_DISPATCH": "1", **RUN_ENV, "OPENCODE_PERMISSION": json.dumps(perm)})
        return p.returncode == 0, (p.stdout or p.stderr or "").strip(), f"{(p.stdout or '')[-2000:]}\n{(p.stderr or '')[-2000:]}"
    except subprocess.TimeoutExpired:
        return False, f"Timeout ({OC_TIMEOUT // 60} min).", f"Timeout ({OC_TIMEOUT // 60} min)."
    except Exception as e:
        return False, f"Error: {e}", f"Error: {e}"


def alert_all_exhausted(primary, reserve, diag):
    """Every engine is out of quota: log + registry + one notification (notify.py: iPhone push + Mac banner), throttled."""
    flat = " ".join((diag or "").split())[-200:]
    claude = f"Claude en cooldown hasta {time.strftime('%H:%M', time.localtime(float(CLAUDE_LIMIT_FILE.read_text().strip())))}" if claude_is_limited() else "Claude sin cooldown registrado"
    log(f"agotado t{CURRENT_TASK} [{CURRENT_GOAL}] opencode={primary} reserva={reserve or '-'} · {claude} · {flat}")
    try:
        subprocess.run([str(Path.home() / "vault/bin/vault-log"), "--agent", "dispatcher", "--kind", "error", "--task", f"t{CURRENT_TASK}",
                        f"IA agotada: {primary} y reserva {reserve or '-'} sin cuota ({CURRENT_GOAL}); {claude}"], check=False, timeout=10)
    except Exception:
        pass
    try:
        last = json.loads(ALERT_FILE.read_text()).get("ts", 0)
    except Exception:
        last = 0
    now = time.time()
    try:
        ALERT_FILE.write_text(json.dumps({"ts": now if now - last >= ALERT_EVERY else last, "last_event": now, "task": CURRENT_TASK, "board": CURRENT_GOAL,
                                          "primary": primary, "reserve": reserve, "claude": claude, "error": flat}))
    except Exception:
        pass
    if now - last >= ALERT_EVERY:
        subprocess.run([sys.executable, str(HERE / "notify.py"), "IA sin cuota: todo agotado",
                        f"OpenCode ({primary}) y la reserva ({reserve or 'ninguna'}) sin cuota. {claude}. t{CURRENT_TASK} queda en ia·fallo: reencola cuando se renueve.",
                        "5", "--task", str(CURRENT_TASK)])


def ask_opencode(prompt, cwd, readonly=False):
    """Run one `opencode run` session with the same guard rails as the claude executor; returns (ok, text).
    readonly (plan mode): no edits and no shell, only its read/search tools.
    Quota / rate-limit / credit failure on OC_MODEL -> one retry on the reserve model (fallback_model_for the board).
    OC_LAST tells the caller which model actually answered."""
    global OC_LAST
    perm = {"bash": "deny", "edit": "deny"} if readonly else OC_PERMISSION
    free = free_model_for(CURRENT_GOAL)
    if opencode_exhausted() and free:  # Go known to be out: straight to the free model
        return ask_free(free, prompt, cwd, perm, "OpenCode Go sin cuota")
    OC_LAST = OC_MODEL
    ok, out, diag = _oc_once(OC_MODEL, prompt, cwd, perm)
    if not is_quota_error(ok, diag):
        return ok, out
    fb = fallback_model_for(CURRENT_GOAL)
    priv = " (tablero sensible: solo OpenCode Go)" if is_sensitive_board(CURRENT_GOAL) else ""
    log(f"t{CURRENT_TASK} opencode cuota/límite en {OC_MODEL} → reserva {fb or '(ninguna)'}{priv} [{CURRENT_GOAL}]")
    if not fb or fb == OC_MODEL:
        alert_all_exhausted(OC_MODEL, "", diag)
        return False, f"{out}\n\n(Cuota/límite agotado en {OC_MODEL} y no hay modelo de reserva para este tablero{priv}. Reencola la tarea cuando se renueve.)"
    OC_LAST = f"{fb} (reserva: cuota agotada en {OC_MODEL})"
    ok2, out2, diag2 = _oc_once(fb, prompt, cwd, perm)
    log(f"t{CURRENT_TASK} reserva {fb} ok={ok2}")
    if ok2:
        return ok2, f"{out2}\n\n(Hecho con el modelo de reserva {fb}: {OC_MODEL} sin cuota.)"
    if is_quota_error(ok2, diag2) or "ECONNREFUSED" in diag2 or "Model unavailable" in diag2:
        alert_all_exhausted(OC_MODEL, fb, diag2)
        if free:
            return ask_free(free, prompt, cwd, perm, f"{OC_MODEL} y la reserva {fb} sin cuota")
        why = "también sin cuota o inaccesible (¿Magpie abierto?)"
    else:
        why = "falló por otro motivo (ver salida)"
    return False, f"{out2}\n\n(Fallo: {OC_MODEL} agotó cuota/crédito y la reserva {fb} {why}. Reencola la tarea cuando se renueve alguna cuota.)"


def ask_free(model, prompt, cwd, perm, why):
    global OC_LAST
    enter_free_mode(model)
    OC_LAST = f"{model} (gratis: {why})"
    ok, out, _ = _oc_once(model, prompt, cwd, perm)
    log(f"t{CURRENT_TASK} gratis {model} ok={ok}")
    return ok, f"{out}\n\n(Hecho con el modelo gratis {model}: {why}.)" if ok else f"{out}\n\n(Falló también el modelo gratis {model}: {why}.)"


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



def _git(repo, *args, timeout=60):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def integrate_to_main(repo_dir, branch, tid=None):
    """Merge `branch` into main from the principal checkout, test, and push if origin exists.

    Only used for MERGE_TO_MAIN_REPOS (Board B, Board V, repo-n). Never --force. Main must be checked out and clean.
    On merge conflict: git merge --abort, log, and signal ia·pregunta (ask=True).
    On npm test failure: reset main to ORIG_HEAD if this call just merged, log, do not push (ask=False).
    Returns (ok, detail, ask).
    """
    repo_dir = Path(repo_dir)
    label = f"t{tid} " if tid is not None else ""
    blob = lambda p: ((p.stdout or "") + (p.stderr or "")).strip()

    if not (repo_dir / ".git").exists():
        msg = f"{label}integrar main: {repo_dir} no es un repo git"
        log(msg)
        return False, msg, False

    try:
        head = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    except Exception as e:
        msg = f"{label}integrar main: git falló ({e})"
        log(msg)
        return False, msg, False
    if head.returncode != 0 or head.stdout.strip() != "main":
        msg = f"{label}integrar main: el repo principal no está en main (HEAD={head.stdout.strip()!r}); no se mergea"
        log(msg)
        return False, msg, True

    st = _git(repo_dir, "status", "--porcelain")
    if st.returncode != 0 or st.stdout.strip():
        msg = f"{label}integrar main: main no está limpio; no se mergea"
        log(msg)
        return False, msg, True

    chk = _git(repo_dir, "rev-parse", "--verify", branch)
    if chk.returncode != 0:
        msg = f"{label}integrar main: la rama {branch} no existe"
        log(msg)
        return False, msg, True

    already = _git(repo_dir, "merge-base", "--is-ancestor", branch, "main").returncode == 0
    if not already:
        # --no-edit: fast-forward when possible, otherwise a merge commit with the default message. Never --force.
        merged = _git(repo_dir, "merge", "--no-edit", branch, timeout=120)
        if merged.returncode != 0:
            _git(repo_dir, "merge", "--abort")
            msg = f"{label}integrar main: conflicto al mergear {branch} en main; merge abortado. {blob(merged)[-2000:]}"
            log(msg)
            return False, msg, True
    else:
        log(f"{label}integrar main: {branch} ya está contenida en main")

    env = {k: v for k, v in os.environ.items() if k not in ("APP_DB", "APP_MODEL")}
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"  # docker lives in ~/.local/bin
    test_cmd = main_test_cmd(repo_dir)
    tname = ("php artisan test (docker sail-8.5)" if test_cmd[0] == "docker" else " ".join(test_cmd)) if test_cmd else "sin tests"
    if test_cmd is None:
        log(f"{label}integrar main: {repo_dir.name} sin package.json en la raíz ni comando de tests; se omite npm test")
        test = subprocess.CompletedProcess([], 0, "", "")
    else:
        try:
            test = subprocess.run(
                test_cmd, cwd=str(repo_dir), capture_output=True, text=True,
                timeout=NPM_TEST_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            if not already:
                _git(repo_dir, "reset", "--hard", "ORIG_HEAD")
            msg = f"{label}integrar main: {tname} excedió {NPM_TEST_TIMEOUT}s; no se hace push"
            log(msg)
            return False, msg, False
        tail = blob(test).splitlines()[-4:]
        log(f"{label}integrar main: tests ({test_cmd[0]}) rc={test.returncode} :: {' | '.join(tail)[:400]}")
    # main was verified clean before the merge, so tracked files modified now were rewritten by the test run itself
    # (repo-v e2e regenerates docs/capturas/*.png): restore them so the next integration does not see a dirty main.
    dirty = [f for f in _git(repo_dir, "diff", "--name-only").stdout.splitlines() if f.strip()]
    if dirty:
        _git(repo_dir, "checkout", "--", *dirty)
        log(f"{label}integrar main: restaurados {len(dirty)} archivos reescritos por los tests")
    if test.returncode != 0:
        if not already:
            _git(repo_dir, "reset", "--hard", "ORIG_HEAD")
        msg = f"{label}integrar main: {tname} falló; no se hace push. {blob(test)[-2000:]}"
        log(msg)
        return False, msg, False

    rem = _git(repo_dir, "remote", "get-url", "origin")
    if rem.returncode != 0 or not rem.stdout.strip():
        tip = _git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
        msg = f"{label}integrar main: {branch} en main ({tip}), tests ok, no hay remoto origin (sin push)"
        log(msg)
        return True, msg, False

    # never --force / --force-with-lease
    push = _git(repo_dir, "push", "origin", "main", timeout=180)
    if push.returncode != 0:
        tip = _git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
        msg = f"{label}integrar main: tests ok, main en {tip}, git push origin main falló (sin --force). {blob(push)[-2000:]}"
        log(msg)
        return False, msg, True
    tip = _git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
    msg = f"{label}integrar main: {branch} en main ({tip}) y push a origin/main"
    log(msg)
    return True, msg, False


def run(api, goal, repo, st, tags, t, mode, trig, runner="claude", resume=None):
    tid = t["id"]
    global CURRENT_TASK, CURRENT_GOAL, RUN_ENV
    CURRENT_TASK = str(tid)
    CURRENT_GOAL = goal.get("name", "")
    if mode == "exec" and runner == "claude" and claude_is_limited() and CFG["handoff_to_opencode"]:
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
    review_base = worktree_base(repo, note) or "main"
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
            pinned = worktree_base(repo, note)
            if pinned:
                start = pinned
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
        if not resume and len(re.sub(r"<[^>]+>", " ", note).strip()) < CONTEXT_MIN and not claude_reserved(goal["name"]):  # thin task: a cheap model gathers context first, fewer questions later
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
                who = f"OpenCode · {OC_LAST}"
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
                    who = f"OpenCode · {OC_LAST}"
                    ok, out = opencode_ok_check(ok, out, wt, review_base)
                else:
                    if resume and attempt == 1:  # continue the session that asked, with the user's answer
                        exec_prompt = f"Respuesta del usuario a tu pregunta:\n{resume['answer']}\n\nContinúa con la tarea y termina con el resumen."
                    o_model, o_budget, o_timeout = note_overrides(note)
                    ok, out, sid, _ = ask(exec_prompt, "acceptEdits", o_model or MODELS["exec"], "exec", wt, agents=EXEC_AGENTS,
                                          resume=resume["sid"] if resume and attempt == 1 else None, budget=o_budget, timeout=o_timeout)
                    who = o_model or MODELS["exec"]
                    if (not ok) and is_claude_limit(out):
                        mark_claude_limited(out)
                        sections.append((f"Resultado de la IA · {who}{tag}", False,
                                         (out or "") + "\n(Límite Claude 5h: paso automático a OpenCode.)"))
                        log(f"t{tid} claude limit mid-exec → opencode")
                        ok, out = ask_opencode(exec_prompt, wt)
                        who = f"OpenCode · {OC_LAST} (fallback límite Claude)"
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
                s_use = usage_now()
                w_use = week_pace(s_use) if s_use else None
                week_hard = bool(w_use and w_use[1] >= WEEK_HARD)
                reserved = claude_reserved(goal["name"])
                if reserved or (s_use and (s_use["five_pct"] >= PACE_HARD or week_hard)):  # hard stop: no new Claude run this late in the window
                    which = (f"reservada ({reserved})" if reserved else f"semanal de Claude al {w_use[1]:.0f}%" if week_hard
                             else f"de 5h de Claude al {s_use['five_pct']:.0f}%")
                    sections.append((f"Revisión · {MODELS['review']}{tag}", False,
                                     f"(Revisión aplazada: ventana {which}; worktree conservado. Reencolar tras el reset.)"))
                    log(f"t{tid} pacing: {which}, review deferred")
                    review_deferred = True
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
    ask_merge = False
    if done_tag == "ia·hecho" and mode == "exec" and not in_place and repo in MERGE_TO_MAIN_REPOS:
        _mok, mdet, ask_merge = integrate_to_main(base, branch, tid)
        body += f"\n\n--- Integración en main ({stamp()}){'' if _mok else ' — FALLÓ'} ---\n{mdet}"
    toggle(api, tags, t, "ia·trabajando", False)
    toggle(api, tags, t, done_tag, True)
    if ask_merge:
        toggle(api, tags, t, "ia·pregunta", True)
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
    text = f"{t['description']}\n{msg} ({done_tag})\n{body[-40000:]}"
    cmd = [str(Path.home() / ".local/bin/send-mail"), "--to", MAIL_TO, "--subject", f"IA: {t['description'][:70]} — {msg}", "--text", text]
    env = {**os.environ, "PATH": f"{HIMALAYA_DIR}:{os.environ.get('PATH', '')}"}
    for attempt, wait in enumerate((0, 30, 90), 1):
        time.sleep(wait)
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        log(f"mail t{t['id']} try {attempt} rc={p.returncode} {(p.stdout + p.stderr).strip()[-200:]}")
        if p.returncode == 0:
            toggle(api, tags, t, MAIL, False)
            return


# --- Settings editable from the orchestrator panel (orq-config.json via orq_config.py), read on every tick ---
# Defaults = the constants above; a bad file or key keeps the default for that key and is logged once per tick.
TICK_FILE = HERE / "logs/.last-tick"


def apply_config(cfg):
    global MERGE_TO_MAIN_REPOS, PRIORITY_PROJECTS, PACE_HOURS, PACE_SLACK, PACE_HARD, WEEK_SLACK, WEEK_HARD, USAGE_ALERTS, MAX_ROUNDS, BUDGET
    MERGE_TO_MAIN_REPOS = set(cfg["merge_to_main_repos"])
    PRIORITY_PROJECTS = list(cfg["priority_projects"])
    PACE_HOURS, PACE_SLACK, PACE_HARD = cfg["pace_hours"], cfg["pace_slack"], cfg["pace_hard"]
    WEEK_SLACK, WEEK_HARD = cfg["week_slack"], cfg["week_hard"]
    USAGE_ALERTS = tuple(cfg["usage_alerts"])
    MAX_ROUNDS = cfg["max_rounds"]
    BUDGET = {k: format(v, "g") for k, v in cfg["budget_usd"].items()}


def priority_goals(api):
    """Goal ids of the priority cards (CFG priority_tasks), so their boards are visited first."""
    out = set()
    for tid in CFG["priority_tasks"][:10]:
        try:
            out.add(api("GET", f"/module/tasks/{int(tid)}")["goalId"])
        except Exception:
            pass  # deleted or not visible: ignored
    return out


CFG, CFG_ERRORS = orq_config.load()
apply_config(CFG)

if __name__ == "__main__":
    main()
