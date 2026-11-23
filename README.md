# taskview-orchestrator

Orquestador de IA para un TaskView self-hosted: recoge tarjetas asignadas a un usuario de IA (Claude Code u OpenCode), las ejecuta en worktrees de git, revisa el resultado, integra en `main` donde está permitido y lo expone todo en un panel web y en una tarjeta de PhoneBox. Incluye las automatizaciones que acompañan al tablero: sincronización con GitHub, healthcheck, resumen diario y semanal, copias de la base de datos y avisos al móvil.

Historial extraído de `taskview-community/selfhost` (fork de TaskView) con `git filter-branch`, así que los commits anteriores conservan sus fechas y mensajes.

## Arquitectura

```
launchd (LaunchAgents com.example.taskview-*)
 ├─ ai-dispatch.py        cada 60 s   despachador: elige tarjeta, lanza claude -p / opencode run, revisa, integra
 │    ├─ orq_config.py    ajustes editables (orq-config.json; sin fichero = valores por defecto)
 │    ├─ ia_users.py      quién es el dueño y quiénes son los usuarios IA (ia-users.json)
 │    └─ ia-packs.json    etiquetas "pack" = modo + correo
 ├─ ai-supervisor.sh      cada 15 min sesión de supervisión (Board B / Board V) con ai-supervisor-prompt.md
 ├─ orq-panel.py          siempre     panel web 127.0.0.1:8790 (orq-panel.html) + push a PhoneBox cada 30 s
 ├─ claude-interactive-handoff.py  cada 60 s   pasa sesiones interactivas a OpenCode al agotar la ventana de 5 h
 ├─ sync.py               08:00       issues/PR de GitHub -> tarjetas
 ├─ daily.py              08:05       resumen y vencimientos al móvil (notify.py)
 ├─ weekly.py             dom 19:00   revisión semanal por correo
 ├─ healthcheck.py        cada hora   API de TaskView y SiteApp en producción
 └─ backup.sh             03:30       pg_dump diario (14 copias); restore-test.sh lo valida
```

Flujo de una tarjeta: se asigna a Claude u OpenCode y se etiqueta `ia·plan` o `ia·ejecutar`. El despachador la pasa a En curso (`ia·trabajando`), trabaja en `~/Projects/code/.ai-worktrees/<repo>-t<id>` en la rama `ai/t<id>` y termina con `ia·plan-listo`, `ia·hecho`, `ia·cambios`, `ia·pregunta` o `ia·fallo`. El detalle está en la cabecera de `ai-dispatch.py`.

Todo se resuelve relativo a la carpeta del script (`HERE`): credenciales, `logs/` y estado local viven junto al código y están en `.gitignore`.

| Fichero | Papel |
|---|---|
| `ai-dispatch.py` | despachador (`--dry-run`, `--usage`, `--integrate-main <repo> <rama>`) |
| `orq-panel.py`, `orq-panel.html` | panel; `--pause` / `--resume` crean o borran `logs/.pausa-orquestador` |
| `phonebox-widget.html`, `phonebox-orq-install.py` | tarjeta y atajos en PhoneBox (`--remove` para deshacer) |
| `bin/estado-orquestador` | estado en terminal, solo lectura (`ORQ_DIR` para otra carpeta) |
| `ai-clean.py` | lista o borra worktrees de tareas hechas |
| `.opencode-*` | modelos de OpenCode (principal, reserva, reserva privada, gratuito) y tableros que no salen de OpenCode Go |
| `php-ini/` | `memory_limit` para los tests PHP de las sesiones |
| `mac-notifier/` | banner nativo de macOS (`build.sh`) |
| `migrate-assignees.py` | migración puntual de asignaciones a usuarios IA |
| `launchd/*.plist.example` | plantillas de los LaunchAgents (`__ORQ_DIR__`, `__HOME__`) |
| `examples/` | plantillas de los ficheros locales que no se versionan |

## Instalación

Requisitos: macOS, Python 3 del sistema (`/usr/bin/python3`, solo librería estándar), TaskView self-hosted en `http://127.0.0.1:1725`, `claude` y `opencode` en el `PATH`, Docker/OrbStack para backups y healthcheck.

1. Credenciales locales (nunca se suben):
   ```sh
   cp examples/credentials.example .credentials && chmod 600 .credentials      # login, password, email
   cp examples/api-token.example .api-token && chmod 600 .api-token            # token de API de TaskView (opcional, preferido)
   ```
2. Opcional: `examples/orq-config.example.json` -> `orq-config.json` (o edítalo desde el panel), `examples/web-tasks.example.txt` -> `web-tasks.txt`, `examples/reserva-claude.example.json` -> `.reserva-claude.json`.
3. Revisa `REPOS`, `MERGE_TO_MAIN_REPOS` y `PRIORITY_PROJECTS` en `ai-dispatch.py` y `REPO_BOARDS` en `sync.py`.
4. LaunchAgents:
   ```sh
   ORQ_DIR="$PWD"
   for f in launchd/*.plist.example; do
     sed -e "s#__ORQ_DIR__#$ORQ_DIR#g" -e "s#__HOME__#$HOME#g" "$f" > ~/Library/LaunchAgents/$(basename "$f" .example)
   done
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.taskview-ai.plist   # y el resto
   ```
5. PhoneBox: `python3 phonebox-orq-install.py`. Terminal: `ln -s "$PWD/bin/estado-orquestador" ~/.local/bin/`.

## Tests

```sh
for t in tests/test_*.py; do python3 "$t" || exit 1; done
# o: python3 -m unittest discover -s tests
```

Los tests usan un `orq-config.json` temporal (`ORQ_CONFIG_FILE`) y nunca tocan el real.

## Pendiente para migrar desde `taskview-community/selfhost`

Hoy los LaunchAgents siguen apuntando a `taskview-community/selfhost`. Antes de apuntarlos aquí:

- `healthcheck.py` levanta TaskView con `docker compose up -d` en su propia carpeta; el `docker-compose.yml` es del despliegue de TaskView y se queda en el fork. Hay que hacer configurable esa carpeta o enlazar el fichero.
- `claude-interactive-handoff.py` tiene rutas fijas a `selfhost/logs/.claude-limit-until` y `selfhost/.opencode-model`.
- `ai-dispatch.py` y `ai-supervisor.sh` usan `~/Projects/code` como raíz de los repos.
- Copiar `.credentials`, `.api-token`, `orq-config.json`, `web-tasks.txt`, `.reserva-claude.json`, `.opencode-model` y, si interesa conservarlo, `logs/`.
- Parar los agentes (`launchctl bootout`), cambiar las rutas, volver a cargarlos y comprobar `bin/estado-orquestador` y el panel.
