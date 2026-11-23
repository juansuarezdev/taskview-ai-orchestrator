#!/bin/bash
# Supervisor opcional de uno o varios tableros: cada 15 min (launchd <prefijo>.taskview-supervisor)
# lanza una sesión `claude -p` (sonnet) que revisa las tarjetas y re-encola las paradas siguiendo el flujo de ai-dispatch.py.
# No programa: solo TaskView MCP + lectura. Prompt: ai-supervisor-prompt.md (local, sin versionar; plantilla en
# examples/ai-supervisor-prompt.example.md). Repos que puede leer con git: supervisor_repos de orq-site.json. Log: logs/ai-supervisor.log.
# --dry-run: la sesión solo lee y cuenta lo que haría (sin herramientas de escritura, sin avisos).
# Para: launchctl bootout gui/$(id -u)/<prefijo>.taskview-supervisor   ·   Fin automático: logs/.supervisor-completado
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGS="$HERE/logs"
LOCK="$LOGS/.ai-supervisor.lock"
if [ -z "${SUP_LOCKED:-}" ]; then  # one at a time: lockf -t 0 fails at once (exit 75) if another run holds the lock
  SUP_LOCKED=1 exec /usr/bin/lockf -k -t 0 "$LOCK" "$0" "$@"
fi
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG="$LOGS/ai-supervisor.log"; DONE="$LOGS/.supervisor-completado"
CLAUDE_LIMIT="$LOGS/.claude-limit-until"; SUP_LIMIT="$LOGS/.supervisor-limit-until"
QUESTIONS="$LOGS/supervisor-preguntas.md"; LAST_NOTIFY="$LOGS/.supervisor-last-notify"
MAX_SECS=900; MAX_TURNS=50; MAX_USD=1.50
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

[ -f "$DONE" ] && exit 0  # everything finished: never launch Claude again
[ -f "$HERE/ai-supervisor-prompt.md" ] || [ -n "${SUP_PRINT_TOOLS:-}" ] || exit 0  # no local prompt: the supervisor is not set up
[ -f "$LOGS/.pausa-orquestador" ] && exit 0  # manual pause from the panel (orq-panel.py)
now=$(date +%s)
for f in "$CLAUDE_LIMIT" "$SUP_LIMIT"; do
  if [ -f "$f" ]; then
    until=$(cut -d. -f1 < "$f" 2>/dev/null); until=${until:-0}
    if [ "$now" -lt "$until" ]; then log "skip: límite de Claude hasta $(date -r "$until" '+%F %T') ($(basename "$f"))"; exit 0; fi
    [ "$f" = "$SUP_LIMIT" ] && rm -f "$SUP_LIMIT"  # own cooldown expired (the dispatcher's file is left to the dispatcher)
  fi
done
# Usage reservation (.reserva-claude.json, see ai-dispatch.py): the supervisor spends Claude too, so it skips the whole window.
if python3 -c 'import json,sys,time
try: w=json.load(open(sys.argv[1]))
except Exception: sys.exit(1)
f=lambda s: time.mktime(time.strptime(s,"%Y-%m-%d %H:%M"))
sys.exit(0 if any(f(x["desde"])<=time.time()<f(x["hasta"]) for x in w) else 1)' "$(dirname "$0")/.reserva-claude.json" 2>/dev/null; then
  log "skip: uso de Claude reservado (.reserva-claude.json)"; exit 0
fi

# Quota saver: if the dispatcher log has not moved since a pass that changed nothing, skip (but run at least once an hour)
STATE="$LOGS/.supervisor-state"; DLOG_HASH=$(tail -n 40 "$LOGS/ai-dispatch.log" 2>/dev/null | /sbin/md5 -q)
if [ "$DRY" = 0 ] && [ -f "$STATE" ]; then
  read -r s_hash s_actions s_ts < "$STATE"
  if [ "$s_hash" = "$DLOG_HASH" ] && [ "${s_actions:-1}" = 0 ] && [ $(( now - ${s_ts:-0} )) -lt 3600 ]; then
    log "skip: sin novedades en el dispatcher desde la última pasada sin cambios"; exit 0
  fi
fi

r() { for c in "$@"; do printf '%s\n' "Bash($c:*)"; done; }
set -f; OLD_IFS="$IFS"; IFS=$'\n'  # $(r …) yields one rule per line; no globbing of the `*`
CODE_ROOT="$(/usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import orq_site; print(orq_site.CODE)' "$HERE")"
SUP_REPOS=($(/usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import orq_site; print("\n".join(str(orq_site.CODE / r) for r in orq_site.SITE["supervisor_repos"]))' "$HERE"))
GIT_READ=(); for d in ${SUP_REPOS[@]+"${SUP_REPOS[@]}"}; do for c in log branch status; do GIT_READ+=("git -C $d $c" "rtk git -C $d $c"); done; done
READ_TOOLS=(Read Glob Grep mcp__taskview__list_goals mcp__taskview__list_tasks mcp__taskview__get_task mcp__taskview__get_task_history
  mcp__taskview__list_tags mcp__taskview__list_kanban_columns mcp__taskview__list_task_dependencies mcp__taskview__list_collaborators_for_goal
  mcp__taskview__list_lists $(r tail ls wc grep ps pgrep "rtk read" "rtk tail" "rtk ls" "rtk wc" "rtk grep" "rtk ps" "rtk pgrep" \
  ${GIT_READ[@]+"${GIT_READ[@]}"}))
WRITE_TOOLS=(mcp__taskview__update_task mcp__taskview__toggle_task_tag mcp__taskview__toggle_task_assignees)
DENIED=(Write Edit MultiEdit NotebookEdit WebFetch WebSearch Task Agent mcp__supabase mcp__engram mcp__plugin_engram_engram
  mcp__taskview__delete_task mcp__taskview__delete_tag mcp__taskview__delete_goal mcp__taskview__delete_list mcp__taskview__delete_kanban_column
  mcp__taskview__delete_task_dependency mcp__taskview__create_task mcp__taskview__create_goal mcp__taskview__restore_task_from_history
  "Read(**/.credentials*)" "Read(**/.api-token)" "Read(**/.env*)" "Read(~/.claude.json)" "Read(~/.claude/.credentials.json)"
  $(r rm "git push" "git merge" "git checkout" "git switch" "git reset" "git rebase" tmux curl launchctl magpie opencode claude sudo \
     "rtk git push" "rtk git merge" "rtk git checkout" "rtk git reset" "rtk rm" "rtk curl"))
IFS="$OLD_IFS"
if [ -n "${SUP_PRINT_TOOLS:-}" ]; then printf "allow: %s\n" "${READ_TOOLS[@]}" "${WRITE_TOOLS[@]}"; printf "deny: %s\n" "${DENIED[@]}"; exit 0; fi
PROMPT="$(cat "$HERE/ai-supervisor-prompt.md")"
if [ "$DRY" = 1 ]; then
  ALLOWED=("${READ_TOOLS[@]}"); DENIED+=("${WRITE_TOOLS[@]}")
  PROMPT="$PROMPT

MODO SIMULACIÓN (--dry-run): NO cambies nada. No llames a update_task, toggle_task_tag ni toggle_task_assignees. Haz todo el análisis y pon en \`acciones\` lo que harías, con el prefijo «haría: »."
else
  ALLOWED=("${READ_TOOLS[@]}" "${WRITE_TOOLS[@]}")
fi
PROMPT="$PROMPT

Hora actual: $(date '+%F %T %Z')."

OUT="$(mktemp -t ai-supervisor)"
cd "$CODE_ROOT" || exit 1
start=$(date +%s)
# ANTHROPIC_BASE_URL unset: the supervisor always talks to Anthropic directly (Max plan), never through a gateway
env -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN TASKVIEW_NOHOOK=1 AI_SUPERVISOR=1 \
  /opt/homebrew/bin/gtimeout -k 30 "$MAX_SECS" claude -p "$PROMPT" --model sonnet --max-turns "$MAX_TURNS" --max-budget-usd "$MAX_USD" \
  --output-format json --json-schema "$(cat "$HERE/ai-supervisor-schema.json")" --no-session-persistence \
  --permission-mode default --allowedTools "${ALLOWED[@]}" --disallowedTools "${DENIED[@]}" > "$OUT" 2> "$OUT.err"
rc=$?
secs=$(( $(date +%s) - start ))

STATE="$STATE" DLOG_HASH="$DLOG_HASH" DRY=$DRY RC=$rc SECS=$secs OUT="$OUT" LOG="$LOG" DONE="$DONE" SUP_LIMIT="$SUP_LIMIT" QUESTIONS="$QUESTIONS" LAST_NOTIFY="$LAST_NOTIFY" HERE="$HERE" \
/usr/bin/python3 - <<'PY'
import json, os, re, subprocess, sys, time
E = os.environ; dry = E["DRY"] == "1"; rc = int(E["RC"]); secs = E["SECS"]
raw = open(E["OUT"]).read(); err = open(E["OUT"] + ".err").read()
def log(m):
    with open(E["LOG"], "a") as f: f.write(f"{time.strftime('%F %T')} {'[dry] ' if dry else ''}{m}\n")
LIMIT = re.compile(r"5-hour limit|hit your limit|usage limit|rate limit|limit reached|out of extra usage", re.I)
try: o = json.loads(raw)
except ValueError: o = {}
text = (o.get("result") or raw or err or "").strip()
cost = o.get("total_cost_usd"); turns = o.get("num_turns")
meta = f"rc={rc} secs={secs} turnos={turns} coste_api_eq=${cost:.2f}" if isinstance(cost, (int, float)) else f"rc={rc} secs={secs} turnos={turns}"
if LIMIT.search(text) or LIMIT.search(err):
    open(E["SUP_LIMIT"], "w").write(str(time.time() + 3600))  # no retry: wait one hour (own file; the dispatcher's is untouched)
    log(f"límite de Claude: salgo sin reintentar, pausa 1 h · {meta}"); sys.exit(0)
if rc == 124:
    log(f"timeout ({secs}s): pasada cortada · {meta}"); sys.exit(0)
s = o.get("structured_output")
if not isinstance(s, dict):
    log(f"sin salida estructurada ({o.get('subtype') or 'error'}): {text[:200]!r} · {meta}"); sys.exit(0)
acc = s.get("acciones") or []; qs = s.get("preguntas_usuario") or []
log(f"{s.get('resumen','').strip()} · acciones={len(acc)} preguntas={len(qs)} · {meta}")
if not dry: open(E["STATE"], "w").write(f"{E['DLOG_HASH']} {len(acc)} {int(time.time())}\n")
for a in acc: log(f"  · {a}")
for q in qs: log(f"  ? {q['tarea']}: {q['pregunta']}")
if qs and not dry:
    path = E["QUESTIONS"]; old = open(path).read() if os.path.exists(path) else "# Preguntas del supervisor\n\nResponde en la nota de la tarjeta (`RESPUESTA: …` + `ia·continuar`).\n"
    new = [q for q in qs if f"{q['tarea']}: {q['pregunta']}" not in old]
    if new:
        with open(path, "w") as f:
            f.write(old.rstrip("\n") + "\n\n" + "\n".join(f"- {time.strftime('%F %H:%M')} {q['tarea']}: {q['pregunta']}" for q in new) + "\n")
        last = float(open(E["LAST_NOTIFY"]).read() or 0) if os.path.exists(E["LAST_NOTIFY"]) else 0
        if time.time() - last >= 3600:  # at most one alert per hour
            m = re.search(r"\d+", new[0]["tarea"])
            subprocess.run([sys.executable, os.path.join(E["HERE"], "notify.py"), "Supervisor IA: necesito una respuesta",
                            f"{len(new)} pregunta(s) nueva(s): {new[0]['tarea']}: {new[0]['pregunta'][:120]}", "3", *(["--task", m.group(0)] if m else [])], timeout=30)
            open(E["LAST_NOTIFY"], "w").write(str(time.time()))
if s.get("completado") and not dry:
    open(E["DONE"], "w").write(time.strftime("%F %T") + "\n")
    log("completado: todas las tarjetas supervisadas terminadas; el supervisor no vuelve a lanzar Claude")
PY
rm -f "$OUT" "$OUT.err"
