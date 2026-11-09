"""Task ownership by assignee, shared by the selfhost scripts.

A task belongs to the user when it is assigned to them, and to an AI when it is assigned to one of the runner users
(ia-users.json). Assignees are collaborator ids, which are per project, so they are mapped back to emails per goal.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = json.loads((HERE / "ia-users.json").read_text())
RUNNERS = {k: v.lower() for k, v in CONF["runners"].items()}  # runner name -> email; dict order = priority when both are assigned
RUNNER_BY_EMAIL = {v: k for k, v in RUNNERS.items()}


def owner_email(creds):
    """The user's email: ia-users.json `owner`, else the `email` line of .credentials."""
    return (CONF.get("owner") or creds.get("email") or "").strip().lower()


def collaborators(api, goal_id):
    """{collaborator id: email} of one project."""
    return {u["id"]: (u.get("email") or "").lower() for u in api("GET", f"/module/collaboration/{goal_id}")}


def assignee_emails(task, collab):
    return {collab[i] for i in (task.get("assignedUsers") or []) if i in collab}


def runner_of(emails):
    """The AI that should run a task assigned to these emails, or None. Claude wins when both are assigned."""
    return next((r for r, e in RUNNERS.items() if e in emails), None)
