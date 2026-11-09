#!/usr/bin/env python3
"""One-off move from ownership tags to assignees. Dry run by default; `--apply` writes.

Per project (archived ones included, so no stale tag is left behind):
  1. The user, Claude and OpenCode (ia-users.json) become collaborators when they are not yet (only if a task needs them).
  2. Tasks tagged `para-mí`      -> assigned to the user, tag removed.
     Tasks tagged `ia·opencode`  -> assigned to OpenCode, `ia·ejecutar` added, `ia·opencode` removed.
     Tasks with ia·plan / ia·ejecutar / a pack tag (not para-mí, not assigned to the user) -> assigned to Claude unless an AI is already assigned;
     the mode tag stays.
     Other assignees are kept. Running it again changes nothing.
  3. The `para-mí` and `ia·opencode` tag definitions are deleted once no task (done ones included) carries them.
Reads every top-level task of the project, done ones included (subtasks come back without tags from the API, so they are
not covered; deleting a tag also drops it from any subtask). Prints the plan per task and a summary per project.
The users must exist first (registered accounts with the emails of ia-users.json).
"""
import json, sys, urllib.parse, urllib.request
from pathlib import Path

import ia_users

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:1725"
OWN, OLD_OC, EXEC = "para-mí", "ia·opencode", "ia·ejecutar"
PACKS = json.loads((HERE / "ia-packs.json").read_text())
MODE_TAGS = {"ia·plan", EXEC, *PACKS}


def call(method, path, body=None, tok=None):
    h = {"content-type": "application/json", "x-taskview-client": "migrate-assignees"}
    if tok:
        h["authorization"] = "Bearer " + tok
    req = urllib.request.Request(API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=20))


def token(c):
    """Scoped API token (selfhost/.api-token) when present, else the password login. Never printed."""
    f = HERE / ".api-token"
    return f.read_text().strip() if f.exists() else call("POST", "/module/auth/login", {"login": c["login"], "password": c["password"]})["access"]


ALL_LISTS = -1401  # ALL_TASKS_LIST_ID: every list of the project


def all_tasks(api, gid):
    """Every top-level task of a project, open and done (the kanban endpoint hides tasks done over 7 days ago)."""
    q = urllib.parse.urlencode({"goalId": gid, "componentId": ALL_LISTS, "page": 0, "showCompleted": 1, "firstNew": 0,
                                "unlimited": "true", "ignoreCompleted": "true"})
    return api("GET", f"/module/tasks?{q}")


def plan_task(names, emails, owner):
    """Changes for one task: (assign emails to add, tag names to add, tag names to remove). Pure, for the dry run."""
    add_who, add_tags, rm_tags = set(), set(), set()
    has_ai = bool(emails & set(ia_users.RUNNERS.values()))
    if OWN in names:
        add_who.add(owner)
        rm_tags.add(OWN)
    elif OLD_OC in names:
        add_who.add(ia_users.RUNNERS["opencode"])
        add_tags.add(EXEC)
        rm_tags.add(OLD_OC)
    elif names & MODE_TAGS and not has_ai and owner not in emails:  # the user's own task is never handed to Claude
        add_who.add(ia_users.RUNNERS["claude"])
    if OLD_OC in names:  # para-mí + ia·opencode: still drop the retired tag (the user's task never runs)
        rm_tags.add(OLD_OC)
    return add_who - emails, add_tags - names, rm_tags & names


def main():
    apply = "--apply" in sys.argv
    c = dict(l.strip().split("=", 1) for l in (HERE / ".credentials").read_text().splitlines() if "=" in l)
    tok = token(c)
    api = lambda m, p, b=None: call(m, p, b, tok)["response"]
    owner = ia_users.owner_email(c)
    if not owner:
        sys.exit("no owner email (ia-users.json owner / .credentials email)")
    print(f"{'APPLY' if apply else 'DRY RUN (use --apply to write)'} · owner={owner} · claude={ia_users.RUNNERS['claude']} · opencode={ia_users.RUNNERS['opencode']}")
    total = {"tasks": 0, "tags_deleted": 0, "collaborators": 0}

    for g in api("GET", "/module/goals"):
        gid = g["id"]
        tags = {t["name"]: t["id"] for t in api("GET", "/module/tags") if t["goalId"] == gid}  # endpoint ignores goalId
        names_of = {v: k for k, v in tags.items()}
        if not ({OWN, OLD_OC, *MODE_TAGS} & tags.keys()):
            continue
        collab = ia_users.collaborators(api, gid)
        by_email = {e: i for i, e in collab.items()}
        changed, added_collab, leftover = 0, [], set()
        print(f"\n[{g['name']}] (id {gid}{', archivado' if g.get('archive') else ''})")
        for t in all_tasks(api, gid):
            names = {names_of[i] for i in (t.get("tags") or []) if i in names_of}
            emails = ia_users.assignee_emails(t, collab)
            who, add_tags, rm_tags = plan_task(names, emails, owner)
            if not (who or add_tags or rm_tags):
                continue
            changed += 1
            parts = [f"asignar {', '.join(sorted(who))}" if who else "", f"+{', +'.join(sorted(add_tags))}" if add_tags else "",
                     f"-{', -'.join(sorted(rm_tags))}" if rm_tags else ""]
            print(f"  t{t['id']} {t['description'][:60]!r}: {' · '.join(p for p in parts if p)}")
            if not apply:
                continue
            for e in who - by_email.keys():  # the assignee must be a collaborator of the project
                u = api("POST", "/module/collaboration", {"goalId": gid, "email": e})
                if not u:
                    raise SystemExit(f"no se pudo añadir {e} como colaborador de {g['name']}")
                by_email[e] = u["id"]
                collab[u["id"]] = e
                added_collab.append(e)
            ids = sorted(set(t.get("assignedUsers") or []) | {by_email[e] for e in who})
            if who:
                api("PATCH", "/module/tasks/task-users", {"taskId": t["id"], "userIds": ids})
            for n in add_tags:
                if n not in tags:
                    tags[n] = api("POST", "/module/tags", {"name": n, "color": "#f97316", "goalId": gid})["id"]
                api("PATCH", "/module/tags/toggle", {"tagId": tags[n], "taskId": t["id"]})
            for n in rm_tags:
                api("PATCH", "/module/tags/toggle", {"tagId": tags[n], "taskId": t["id"]})
        # tag definitions go only when no task still carries them (re-read after the changes)
        for n in (OWN, OLD_OC):
            if n not in tags:
                continue
            still = [t["id"] for t in (all_tasks(api, gid) if apply else []) if tags[n] in (t.get("tags") or [])]
            if still:
                leftover.add(n)
                print(f"  etiqueta {n} se queda: aún la llevan {still}")
            elif apply:
                api("DELETE", "/module/tags", {"tagId": tags[n]})
                total["tags_deleted"] += 1
                print(f"  etiqueta {n} borrada")
            else:
                print(f"  etiqueta {n}: se borrará cuando ninguna tarea la lleve")
        total["tasks"] += changed
        total["collaborators"] += len(added_collab)
        print(f"  resumen: {changed} tarea(s) {'cambiadas' if apply else 'a cambiar'}"
              + (f", colaboradores añadidos: {', '.join(sorted(set(added_collab)))}" if added_collab else "")
              + (f", etiquetas pendientes: {', '.join(sorted(leftover))}" if leftover else ""))
    print(f"\nTotal: {total['tasks']} tarea(s), {total['collaborators']} colaborador(es) añadidos, {total['tags_deleted']} etiqueta(s) borradas"
          + ("" if apply else " (nada escrito)"))


if __name__ == "__main__":
    main()
