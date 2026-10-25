#!/usr/bin/env python3
"""Hourly health check for the self-hosted TaskView.

- API down: try `docker compose up -d` (Docker daemon permitting).
- Latest DB backup tiny (< 1 KB, an empty gzip is 20 bytes): rerun backup.sh.
- Anything still wrong afterwards: macOS notification.
"""
import subprocess, sys, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BACKUPS = Path.home() / "Backups/taskview"



def sh(*cmd, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def api_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:1725/", timeout=3)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP answer means the API is alive
    except Exception:
        return False


def latest_backup_size():
    files = sorted(BACKUPS.glob("taskview-*.sql.gz"))
    return files[-1].stat().st_size if files else 0


def notify(msg):
    sh("osascript", "-e", f'display notification "{msg}" with title "TaskView"')


def main():
    problems = []
    if not api_up():
        sh("docker", "compose", "up", "-d", cwd=HERE)
        if not api_up():
            problems.append("la API no responde")
    if api_up() and latest_backup_size() < 1024:
        sh("bash", str(HERE / "backup.sh"))
        if latest_backup_size() < 1024:
            problems.append("el último backup está vacío")
    if problems:
        notify("Fallo: " + " y ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
