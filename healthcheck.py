#!/usr/bin/env python3
"""Hourly health check for the self-hosted TaskView.

- API down: try `docker compose up -d` (Docker daemon permitting).
- Latest DB backup tiny (< 1 KB, an empty gzip is 20 bytes): rerun backup.sh.
- Latest backup less than half the size of the previous one (data loss?): alert.
- Once a week: restore the latest backup into a throwaway Postgres (restore-test.sh) and alert if it does not restore.
- Anything still wrong afterwards: macOS notification.
- Board C in production (https://app.example.com), from outside only (no SSH): /up and /login
  must answer 200 and the TLS certificate must have at least 14 days left; separate alert.
"""
import socket, ssl, subprocess, sys, time, urllib.error, urllib.request
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


def notify(msg, title="TaskView"):
    sh("osascript", "-e", f'display notification "{msg}" with title "{title}"')
    sh(sys.executable, str(HERE / "notify.py"), title, msg, "4")


SITE_HOST = "app.example.com"
TLS_MIN_DAYS = 14


def http_status(url):
    try:
        return urllib.request.urlopen(url, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def tls_days_left(host):
    """Whole days until the certificate expires, or None when the TLS handshake fails."""
    try:
        with socket.create_connection((host, 443), timeout=10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                not_after = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
        return int((not_after - time.time()) // 86400)
    except Exception:
        return None


def siteapp_problems(status=http_status, days_left=tls_days_left):
    """What is wrong with SiteApp in production, seen from outside. Empty list = healthy."""
    problems = []
    for path in ("/up", "/login"):
        code = status(f"https://{SITE_HOST}{path}")
        if code != 200:
            problems.append(f"{path} responde {code or 'nada'}")
    days = days_left(SITE_HOST)
    if days is None:
        problems.append("el certificado TLS no valida")
    elif days < TLS_MIN_DAYS:
        problems.append(f"al certificado TLS le quedan {days} días")
    return problems


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
    siteapp = siteapp_problems()
    if siteapp:
        notify("Producción: " + " y ".join(siteapp), "SiteApp")
    if problems:
        notify("Fallo: " + " y ".join(problems))
    return 1 if problems or siteapp else 0


if __name__ == "__main__":
    sys.exit(main())
