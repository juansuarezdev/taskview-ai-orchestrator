"""Site-specific settings: which boards map to which repos, where they live, who gets mail, what to health-check.

Values live in orq-site.json next to the scripts (gitignored; template in examples/orq-site.example.json).
A missing file or key falls back to DEFAULTS, which are deliberately empty: nothing personal is hard-coded.
A few keys can also come from the environment (they win over the file):

  ORQ_SITE_FILE      path of the JSON file
  ORQ_MAIL_TO        mail_to
  ORQ_CODE_ROOT      code_root
  ORQ_TS_HOST        tailnet_host
  ORQ_HEALTH_HOST    health_host
  ORQ_GITHUB_OWNER   github_owner
  ORQ_EVENT_LOG_CMD  event_log_cmd
  ORQ_LABEL_PREFIX   launchd_prefix
"""
import copy
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = Path(os.environ.get("ORQ_SITE_FILE") or HERE / "orq-site.json")

DEFAULTS = {
    "mail_to": "",                 # recipient of ia·correo results, daily and weekly mails ("" = no mail)
    "code_root": "~/Projects/code",  # folder holding one checkout per repo, plus .ai-worktrees/
    "repos": {},                   # TaskView board name -> repo folder name under code_root
    "extra_roots": {},             # repo -> folder when it is not <code_root>/<repo> (no git there = plan only)
    "board_base": {},              # repo -> default base branch for new ai/t<id> worktrees
    "main_test_cmd": {},           # repo -> argv run on main before pushing (default: npm test when package.json exists)
    "laravel_docker_test": {},     # repo -> subfolder with a Laravel app tested in the sail image before pushing
    "test_env_repos": [],          # Laravel + Sail repos whose worktree tests use the Sail Postgres `testing` DB
    "repo_extra_bash": {},         # repo -> extra Bash command prefixes allowed in its sessions
    "repo_bash_all": [],           # repos whose sessions get unrestricted Bash (global deny list still applies)
    "repo_extra_denied": {},       # repo -> extra Bash command prefixes denied in its sessions
    "sensitive_boards": [],        # fallback when .opencode-fallback-exclude is empty (fnmatch patterns)
    "github_owner": "",            # sync.py: GitHub user whose issues/PRs become cards
    "github_repo_boards": {},      # sync.py: repo folder -> board name
    "clean_repos": [],             # ai-clean.py: repos whose worktrees it lists
    "tailnet_host": "",            # orq-panel.py: Tailscale Serve host allowed besides localhost ("" = localhost only)
    "tailnet_port": 8455,
    "health_host": "",             # healthcheck.py: production host to probe (/up, /login, TLS) ("" = skip)
    "event_log_cmd": "",           # optional command that receives dispatcher start/end events ("" = none)
    "launchd_prefix": "com.example",  # LaunchAgent labels are <prefix>.taskview-<name>
    "supervisor_repos": [],        # ai-supervisor.sh: repos the supervisor session may inspect with read-only git
    "local_delegate_usage": "",    # weekly.py: optional usage.jsonl of a local model delegate ("" = skip)
}
ENV = {"mail_to": "ORQ_MAIL_TO", "code_root": "ORQ_CODE_ROOT", "tailnet_host": "ORQ_TS_HOST", "health_host": "ORQ_HEALTH_HOST",
       "github_owner": "ORQ_GITHUB_OWNER", "event_log_cmd": "ORQ_EVENT_LOG_CMD", "launchd_prefix": "ORQ_LABEL_PREFIX"}


def load(path=None):
    """Settings dict. Never raises: an unreadable file gives the defaults (plus env overrides)."""
    out = copy.deepcopy(DEFAULTS)
    try:
        raw = json.loads(Path(path or FILE).read_text())
        if isinstance(raw, dict):
            out.update({k: v for k, v in raw.items() if k in DEFAULTS and isinstance(v, type(DEFAULTS[k]))})
    except (OSError, ValueError):
        pass
    for k, var in ENV.items():
        if os.environ.get(var):
            out[k] = os.environ[var]
    return out


SITE = load()
CODE = Path(os.path.expanduser(SITE["code_root"]))


def path(p):
    return Path(os.path.expanduser(p))
