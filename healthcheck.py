#!/usr/bin/env python3
"""Hourly health check for the self-hosted TaskView.

- API down: try `docker compose up -d` (Docker daemon permitting).
- Latest DB backup tiny (< 1 KB, an empty gzip is 20 bytes): rerun backup.sh.
- Latest backup less than half the size of the previous one (data loss?): alert.
- Once a week: restore the latest backup into a throwaway Postgres (restore-test.sh) and alert if it does not restore.
- Anything still wrong afterwards: macOS notification.
"""
import subprocess, sys, time, urllib.error, urllib.request
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


def backup_shrank():
    files = sorted(BACKUPS.glob("taskview-*.sql.gz"))
    return len(files) > 1 and files[-1].stat().st_size < files[-2].stat().st_size / 2


def restore_due():
    """True when the last weekly restore test is older than 7 days; the marker is touched by a passing test."""
    marker = HERE / "logs/.restore-test"
    return not marker.exists() or time.time() - marker.stat().st_mtime > 7 * 86400


def notify(msg):
    sh("osascript", "-e", f'display notification "{msg}" with title "TaskView"')
    sh(sys.executable, str(HERE / "notify.py"), "TaskView", msg, "4")


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
    if api_up() and backup_shrank():
        problems.append("el backup de hoy es menos de la mitad que el anterior")
    if api_up() and restore_due():
        if sh("bash", str(HERE / "restore-test.sh")).returncode == 0:
            (HERE / "logs/.restore-test").touch()
        else:
            problems.append("el backup no se restaura bien (restore-test.sh)")
    if problems:
        notify("Fallo: " + " y ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
