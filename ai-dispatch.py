#!/usr/bin/env python3
"""Run TaskView tasks tagged for an AI agent.

Tag a task (not `para-mí`, status Backlog/TODO) in a project board with:
  ia·plan      -> claude plans only (read-only), writes the plan into the task note, moves it to Review
  ia·opencode  -> same as ia·ejecutar but OpenCode implements (free model opencode/deepseek-v4.1-flash-free); Opus still reviews
  ia·ejecutar  -> claude implements it in its own git worktree/branch `ai/t<id>`, never pushes, moves it to Review
Models: plan = Opus, exec = Sonnet, then an automatic review by Opus (ia·hecho if approved, ia·cambios if not).
Plan/exec sessions delegate to subagents by model: `explorer` (Haiku, read-only search) and `tester` (Sonnet, runs tests).
The review answers with a JSON schema (verdict + findings) instead of free text.
Questions: a claude session that lacks information ends with `PREGUNTA: ...`; the task goes to Review with ia·pregunta and the
session id in the note. Answer by writing `RESPUESTA: ...` below the `Sesión IA:` line and tag it ia·continuar: the session is resumed.
Progress tags: ia·trabajando while running, then ia·plan-listo / ia·hecho / ia·cambios / ia·fallo / ia·pregunta.
Tag ia·correo on a task: when its run ends the result is emailed (send-mail) and the tag is removed.
One task at a time (flock). Run every minute from launchd. `--dry-run` only lists.
"""
import fcntl, html, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"
CODE = Path.home() / "Projects/code"
EXTRA_ROOTS = {"repo-r": Path.home() / "Projects/repo-r"}  # boards whose folder lives outside ~/Projects/code (not a git repo: plan only)
WORKTREES = CODE / ".ai-worktrees"
LOG = HERE / "logs/ai-dispatch.log"
REPOS = {"Board M": "repo-m", "Board G": "repo-g", "Board E": "repo-e", "TaskView": "taskview-community", "Board R": "repo-r"}
TRIGGERS = {"ia·plan": ("plan", "claude"), "ia·ejecutar": ("exec", "claude"), "ia·opencode": ("exec", "opencode")}
MAIL = "ia·correo"  # per-task: email the result to MAIL_TO when the run ends, then the tag is removed
MAIL_TO = "you@example.com"
HIMALAYA_DIR = "/opt/homebrew/bin"  # launchd PATH does not include it
CONTINUE = "ia·continuar"  # set by the user on a Review task after writing RESPUESTA: in the note
PROGRESS = {"ia·trabajando": "#f59e0b", "ia·plan-listo": "#3b82f6", "ia·hecho": "#22c55e", "ia·cambios": "#eab308", "ia·fallo": "#ef4444",
            "ia·pregunta": "#a855f7"}
TRIGGER_COLORS = {"ia·plan": "#3b82f6", "ia·ejecutar": "#f97316", "ia·opencode": "#10b981", CONTINUE: "#a855f7", MAIL: "#0ea5e9"}
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
MAX_ROUNDS = 3  # execute + review rounds when the review asks for changes
BUDGET = {"plan": "2", "exec": "5", "review": "2"}
TIMEOUT = {"plan": 15 * 60, "exec": 40 * 60, "review": 15 * 60}
ALLOWED = ["Read", "Glob", "Grep", "Edit", "Write", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)",
           "Bash(git commit:*)", "Bash(ls:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(pnpm:*)", "Bash(python3:*)", "Bash(node:*)"]
WEB = ["WebSearch", "WebFetch"]  # analysis (plan) sessions may research the web
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

def token(c):
    """Scoped API token (selfhost/.api-token, revocable from the web UI) when present, else the password login."""
    f = HERE / ".api-token"
    return f.read_text().strip() if f.exists() else call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]


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
        if not any(n in tags for n in (*TRIGGERS, CONTINUE)):
            continue
        st = {s["name"]: s["id"] for s in api("POST", "/module/kanban/fetch-statuses", {"goalId": gid})}
        for sname in ("TODO", "Backlog", "Review"):
            if sname not in st:
                continue
            cur = "null"
            while cur:
                r = api("GET", f"/module/kanban/tasks/{gid}/{st[sname]}/{cur}")
                for t in r["tasks"]:
                    have = set(t.get("tags") or [])
                    if tags.get("para-mí") in have:
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
                            subprocess.run([sys.executable, str(HERE / "notify.py"), f"IA: {t['description'][:60]}", f"No puedo continuar: {err}", "3"])
                        else:
                            run(api, g, repo, st, tags, t, res["mode"], CONTINUE, res["runner"], res)
                            return
                        continue
                    hit = next(((n, m, rn) for n, (m, rn) in TRIGGERS.items() if tags.get(n) in have), None)
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


def ask(prompt, perm, model, mode, cwd, agents=None, schema=None, resume=None):
    """Run one claude -p session; returns (ok, text, session_id, structured_output)."""
    extra = [*(["--agents", json.dumps(agents)] if agents else []), *(["--json-schema", json.dumps(schema)] if schema else []),
             *(["--resume", resume] if resume else [])]
    try:
        p = subprocess.run(["claude", "-p", prompt, "--model", model, "--permission-mode", perm, "--max-budget-usd", BUDGET[mode],
                            "--output-format", "json", *extra,
                            "--allowedTools", *ALLOWED, *(WEB if perm == "plan" else []), "--disallowedTools", *DENIED],
                           cwd=cwd, env={**os.environ, "TASKVIEW_NOHOOK": "1"}, capture_output=True, text=True, timeout=TIMEOUT[mode])
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
OC_PERMISSION = {"bash": {"*": "allow", "git push*": "deny", "git reset*": "deny", "rm *": "deny", "sudo*": "deny", "curl*": "deny", "wget*": "deny"},
                 "edit": "allow"}


def ask_opencode(prompt, cwd):
    """Run one `opencode run` session with the same guard rails as the claude executor; returns (ok, text)."""
    try:
        p = subprocess.run([OC_BIN, "run", "--model", OC_MODEL, "--dir", str(cwd), prompt], cwd=cwd, capture_output=True, text=True,
                           timeout=OC_TIMEOUT, env={**os.environ, "TASKVIEW_NOHOOK": "1", "OPENCODE_PERMISSION": json.dumps(OC_PERMISSION)})
        return p.returncode == 0, (p.stdout or p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"Timeout ({OC_TIMEOUT // 60} min)."
    except Exception as e:
        return False, f"Error: {e}"


def run(api, goal, repo, st, tags, t, mode, trig, runner="claude", resume=None):
    tid = t["id"]
    log(f"start t{tid} {mode} [{goal['name']}] {t['description']}")
    toggle(api, tags, t, trig, False)
    toggle(api, tags, t, "ia·trabajando", True)
    toggle(api, tags, t, "ia·pregunta", False)
    api("PATCH", "/module/tasks", {"id": tid, "statusId": st["In Progress"]})
    note = t.get("note") or ""
    base = EXTRA_ROOTS.get(repo, CODE / repo)
    in_place = not (base / ".git").exists()  # no repo: no worktree, read-only analysis only
    branch, wt = f"ai/t{tid}", WORKTREES / f"{repo}-t{tid}"
    ctx = board_context(api, goal, st, t)
    stamp = lambda: time.strftime("%F %H:%M")
    sections, ok, verdict_ok, asked = [], False, True, None  # asked = session id of a question waiting for the user
    try:
        if in_place:
            if mode != "plan":
                raise RuntimeError(f"{base} no es un repo git: solo se admite ia·plan (análisis sin cambios)")
            wt = base
        elif not wt.exists():
            WORKTREES.mkdir(exist_ok=True)
            start = "main" if subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", "main"], capture_output=True).returncode == 0 else "HEAD"
            subprocess.run(["git", "-C", str(base), "worktree", "add", "-b", branch, str(wt), start], check=True, capture_output=True)
        task_txt = f"Tarea de TaskView #{tid}: {t['description']}\n\nNotas de la tarea:\n{note}\n"
        if mode == "plan":
            if resume:
                prompt = f"Respuesta del usuario a tu pregunta:\n{resume['answer']}\n\nContinúa y entrega el plan completo."
            else:
                prompt = (f"{ctx}\n{task_txt}\nAnaliza el repo y escribe un plan de implementación concreto: archivos a tocar, pasos, riesgos y cómo verificarlo. "
                          f"NO modifiques nada. {DELEGATE.replace(' y la ejecución de tests/build en el subagente `tester`', '')}{ASK_RULE}Responde en español, conciso, en markdown.")
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
                else:
                    if resume and attempt == 1:  # continue the session that asked, with the user's answer
                        exec_prompt = f"Respuesta del usuario a tu pregunta:\n{resume['answer']}\n\nContinúa con la tarea y termina con el resumen."
                    ok, out, sid, _ = ask(exec_prompt, "acceptEdits", MODELS["exec"], "exec", wt, agents=EXEC_AGENTS,
                                          resume=resume["sid"] if resume and attempt == 1 else None)
                    who = MODELS["exec"]
                sections.append((f"Resultado de la IA · {who}{tag}", ok, out))
                if ok and runner == "claude" and question(out):
                    asked = sid  # leave the worktree as is; the resumed session keeps working on it
                    break
                if ok and subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
                    # headless sessions cannot get approval for git add/commit, so the dispatcher commits what the model left behind
                    subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
                    c = subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", t["description"][:72] + (f" (ronda {attempt})" if attempt > 1 else "")],
                                       capture_output=True, text=True)
                    sections.append(("Commit del dispatcher", c.returncode == 0, c.stdout + c.stderr or t["description"][:72]))
                if not ok:
                    break
                rok, rout, _, so = ask(f"{ctx}\n{task_txt}\nOtra IA implementó esta tarea en la rama {branch} (worktree actual). Revisa con `git diff main...HEAD` y `git log main..HEAD`: "
                                "¿cumple lo pedido?, ¿bugs, casos límite, seguridad o cambios fuera de alcance?, ¿faltan tests? NO modifiques nada. "
                                "Responde con el veredicto y los hallazgos concretos (archivo y línea) en el esquema pedido.",
                                      "plan", MODELS["review"], "review", wt, agents=PLAN_AGENTS, schema=REVIEW_SCHEMA)
                if so:
                    verdict_ok, rout = rok and so["veredicto"] == "APROBADO", fmt_review(so)
                else:  # no structured output: fall back to the text
                    verdict_ok = rok and "VEREDICTO: APROBADO" in rout
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
    done_tag = "ia·fallo" if not ok else ("ia·pregunta" if asked else "ia·plan-listo" if mode == "plan" else ("ia·hecho" if verdict_ok else "ia·cambios"))
    toggle(api, tags, t, "ia·trabajando", False)
    toggle(api, tags, t, done_tag, True)
    api("PATCH", "/module/tasks", {"id": tid, "note": f"{note}{body}".strip(), "statusId": st["Review"]})
    log(f"end t{tid} ok={ok} {done_tag}")
    msg = {"ia·fallo": "Revisa la nota de la tarea", "ia·plan-listo": "Plan listo para revisar", "ia·hecho": "Implementada y aprobada por la revisión",
           "ia·cambios": "Implementada, la revisión pide cambios", "ia·pregunta": "La IA tiene una pregunta"}[done_tag]
    subprocess.run([sys.executable, str(HERE / "notify.py"), f"IA: {t['description'][:60]}", msg, "4" if done_tag in ("ia·fallo", "ia·cambios") else "3"])
    if tags.get(MAIL) in set(t.get("tags") or []):
        mail_result(api, tags, t, msg, body, done_tag)


def mail_result(api, tags, t, msg, body, done_tag):
    """Email the outcome of one run; the tag is removed only when the mail went out."""
    text = f"{t['description']}\n{msg} ({done_tag})\n{body[-8000:]}"
    p = subprocess.run([str(Path.home() / ".local/bin/send-mail"), "--to", MAIL_TO, "--subject", f"IA: {t['description'][:70]} — {msg}", "--text", text],
                       capture_output=True, text=True, env={**os.environ, "PATH": f"{HIMALAYA_DIR}:{os.environ.get('PATH', '')}"})
    log(f"mail t{t['id']} rc={p.returncode} {(p.stdout + p.stderr).strip()[-200:]}")
    if p.returncode == 0:
        toggle(api, tags, t, MAIL, False)


if __name__ == "__main__":
    main()
