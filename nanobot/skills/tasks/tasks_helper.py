#!/usr/bin/env python3
"""CLI helper for nanobot task management (Obsidian Tasks Dataview format).

Standalone script to view and manage tasks from the command line,
independent of the nanobot agent. Operates on the same markdown task
files in ~/.nanobot/workspace/memory/tasks/.

Usage:
    tasks_helper.py list [GROUP]        List tasks (GROUP defaults to 'tasks', '*' for all)
    tasks_helper.py add TITLE [--group GROUP] [--priority PRIO] [--due DATE]
    tasks_helper.py done TASK_ID [--group GROUP]
    tasks_helper.py reopen TASK_ID [--group GROUP]
    tasks_helper.py update TASK_ID [--group GROUP] [--status STATUS] [--priority PRIO] [--due DATE]
    tasks_helper.py archive [GROUP]     Archive completed tasks (GROUP defaults to 'tasks')
    tasks_helper.py show TASK_ID [--group GROUP]  Show full task details
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

TASKS_DIR = Path.home() / ".nanobot" / "workspace" / "memory" / "tasks"
ARCHIVE_DIR = TASKS_DIR / "archive"

VALID_STATUSES = {"todo", "in-progress", "done", "cancelled", "blocked"}
VALID_PRIORITIES = {"lowest", "low", "medium", "high", "highest"}

# Status symbols
STATUS_SYMBOLS = {" ": "todo", "x": "done", "/": "in-progress", "-": "cancelled", "?": "blocked"}
STATUS_TO_SYMBOL = {v: k for k, v in STATUS_SYMBOLS.items()}

# Regexes
TASK_RE = re.compile(r"^[-*+]\s+\[(.)\]\s+(.+)$")
FIELD_RE = re.compile(r"\[(\w+)::\s*([^\]]*)\]")

# ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"

STATUS_COLORS = {
    "todo": C_CYAN, "in-progress": C_YELLOW, "done": C_GREEN,
    "cancelled": C_DIM, "blocked": C_RED,
}
PRIO_COLORS = {"highest": C_RED, "high": C_RED, "medium": C_YELLOW, "low": C_DIM, "lowest": C_DIM}


def slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")[:80] or "untitled"


def parse_task_line(line: str) -> dict | None:
    m = TASK_RE.match(line.strip())
    if not m:
        return None
    symbol, rest = m.group(1), m.group(2)
    status = STATUS_SYMBOLS.get(symbol, "todo")
    fields = {}
    for fm in FIELD_RE.finditer(rest):
        fields[fm.group(1)] = fm.group(2).strip()
    desc = FIELD_RE.sub("", rest).strip()
    task = {"title": desc, "status": status}
    task.update(fields)
    return task


def render_task_line(task: dict) -> str:
    symbol = STATUS_TO_SYMBOL.get(task.get("status", "todo"), " ")
    title = task.get("title", "untitled")
    field_order = [
        "priority", "created", "scheduled", "start", "due",
        "completion", "cancelled", "repeat", "id", "dependsOn",
    ]
    fields = []
    seen = set()
    for key in field_order:
        if key in task:
            fields.append(f"[{key}:: {task[key]}]")
            seen.add(key)
    for key, val in task.items():
        if key not in ("title", "status") and key not in seen:
            fields.append(f"[{key}:: {val}]")
    suffix = "  " + "  ".join(fields) if fields else ""
    return f"- [{symbol}] {title}{suffix}"


def parse_task_file(text: str) -> tuple[list[str], list[dict]]:
    header, tasks = [], []
    in_header = True
    for line in text.split("\n"):
        task = parse_task_line(line)
        if task:
            in_header = False
            tasks.append(task)
        elif in_header:
            header.append(line)
    return header, tasks


def render_task_file(header: list[str], tasks: list[dict]) -> str:
    lines = list(header)
    if lines and lines[-1].strip():
        lines.append("")
    for task in tasks:
        lines.append(render_task_line(task))
    return "\n".join(lines) + "\n"


def load_group(group: str) -> tuple[list[str], list[dict]]:
    path = group_path(group)
    if not path.exists():
        return [], []
    return parse_task_file(path.read_text())


def save_group(group: str, tasks: list[dict], header: list[str] | None = None) -> None:
    path = group_path(group)
    if not tasks:
        if path.exists():
            path.unlink()
        return
    path.write_text(render_task_file(header or [], tasks))


def find_task(tasks: list[dict], tid: str) -> tuple[int, dict] | None:
    tid = tid.lower()
    for i, t in enumerate(tasks):
        slug = slugify(t.get("title", ""))
        title = t.get("title", "").lower()
        if slug == tid or tid in title or tid in slug:
            return i, t
    return None


def group_path(group: str) -> Path:
    safe = re.sub(r"[^\w-]", "", group.lower())
    return TASKS_DIR / f"{safe}.md"


def fmt_status(status: str) -> str:
    color = STATUS_COLORS.get(status, "")
    return f"{color}{status}{C_RESET}"


def fmt_prio(prio: str) -> str:
    color = PRIO_COLORS.get(prio, "")
    return f"{color}{prio}{C_RESET}" if color else prio


def cmd_list(args: argparse.Namespace) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    group = args.group or "tasks"

    if group == "*":
        files = sorted(TASKS_DIR.glob("*.md"))
        if not files:
            print("No task groups found.")
            return
        print(f"{C_BOLD}Task Groups:{C_RESET}")
        for f in files:
            _, tasks = load_group(f.stem)
            open_c = sum(1 for t in tasks if t.get("status") != "done")
            done_c = sum(1 for t in tasks if t.get("status") == "done")
            print(f"  {C_CYAN}{f.stem}{C_RESET}  {open_c} open, {done_c} done")
        archived = list(ARCHIVE_DIR.glob("*.md"))
        if archived:
            print(f"\n  {C_DIM}Archived: {len(archived)} groups{C_RESET}")
        return

    _, tasks = load_group(group)
    if not tasks:
        print(f"No tasks in '{group}'.")
        return

    print(f"{C_BOLD}Tasks in '{group}':{C_RESET}")
    for t in tasks:
        status = t.get("status", "todo")
        prio = t.get("priority", "")
        title = t.get("title", "?")
        due = t.get("due", "")
        slug = slugify(title)
        parts = [f"  [{fmt_status(status)}] {title}"]
        if prio:
            parts.append(f" {C_DIM}({fmt_prio(prio)}){C_RESET}")
        if due:
            parts.append(f"  {C_DIM}due: {due}{C_RESET}")
        parts.append(f"  {C_DIM}id: {slug}{C_RESET}")
        print("".join(parts))


def cmd_add(args: argparse.Namespace) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    group = args.group or "tasks"
    header, tasks = load_group(group)
    slug = slugify(args.title)
    for t in tasks:
        if slugify(t.get("title", "")) == slug:
            print(f"Error: task '{args.title}' already exists in '{group}'", file=sys.stderr)
            sys.exit(1)
    task = {"title": args.title, "status": "todo", "created": str(date.today())}
    if args.priority:
        task["priority"] = args.priority
    if args.due:
        task["due"] = args.due
    tasks.append(task)
    save_group(group, tasks, header)
    print(f"Created '{args.title}' in '{group}'  {C_DIM}(id: {slug}){C_RESET}")


def cmd_done(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    header, tasks = load_group(group)
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    idx, task = result
    task["status"] = "done"
    task["completion"] = str(date.today())
    tasks[idx] = task
    save_group(group, tasks, header)
    print(f"{C_GREEN}Completed:{C_RESET} {task.get('title')}")
    if group != "tasks" and all(t.get("status") == "done" for t in tasks):
        print(f"{C_YELLOW}All tasks in '{group}' done. Run: tasks_helper.py archive {group}{C_RESET}")


def cmd_reopen(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    header, tasks = load_group(group)
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    idx, task = result
    task["status"] = "todo"
    task.pop("completion", None)
    tasks[idx] = task
    save_group(group, tasks, header)
    print(f"Reopened: {task.get('title')}")


def cmd_update(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    header, tasks = load_group(group)
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    idx, task = result
    if args.status:
        if args.status not in VALID_STATUSES:
            print(f"Error: invalid status '{args.status}'", file=sys.stderr)
            sys.exit(1)
        task["status"] = args.status
        if args.status == "done":
            task["completion"] = str(date.today())
    if args.priority:
        if args.priority not in VALID_PRIORITIES:
            print(f"Error: invalid priority '{args.priority}'", file=sys.stderr)
            sys.exit(1)
        task["priority"] = args.priority
    if args.due:
        task["due"] = args.due
    tasks[idx] = task
    save_group(group, tasks, header)
    print(f"Updated: {task.get('title')}")


def cmd_archive(args: argparse.Namespace) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    group = args.group or "tasks"

    if group == "tasks":
        header, tasks = load_group(group)
        done = [t for t in tasks if t.get("status") == "done"]
        remaining = [t for t in tasks if t.get("status") != "done"]
        if not done:
            print("No completed tasks to archive.")
            return
        archive_path = ARCHIVE_DIR / f"tasks-{date.today()}.md"
        existing = archive_path.read_text() if archive_path.exists() else ""
        archive_path.write_text(existing + render_task_file([], done))
        save_group(group, remaining, header)
        print(f"Archived {len(done)} completed tasks.")
    else:
        src = group_path(group)
        if not src.exists():
            print(f"Error: group '{group}' not found", file=sys.stderr)
            sys.exit(1)
        dst = ARCHIVE_DIR / src.name
        shutil.move(str(src), str(dst))
        print(f"Archived group '{group}'")


def cmd_show(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    _, tasks = load_group(group)
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    _, task = result
    print(f"{C_BOLD}{task.get('title', '?')}{C_RESET}")
    print(f"  Status:   {fmt_status(task.get('status', 'todo'))}")
    if task.get("priority"):
        print(f"  Priority: {fmt_prio(task['priority'])}")
    print(f"  Created:  {task.get('created', '?')}")
    if task.get("due"):
        print(f"  Due:      {task['due']}")
    if task.get("scheduled"):
        print(f"  Scheduled: {task['scheduled']}")
    if task.get("start"):
        print(f"  Start:    {task['start']}")
    if task.get("completion"):
        print(f"  Completed: {task['completion']}")
    # Show the raw line
    print(f"\n  {C_DIM}{render_task_line(task)}{C_RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Nanobot task manager (Dataview format)")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", aliases=["ls"], help="List tasks")
    p_list.add_argument("group", nargs="?", default="tasks")

    p_add = sub.add_parser("add", help="Add a task")
    p_add.add_argument("title")
    p_add.add_argument("--group", "-g", default="tasks")
    p_add.add_argument("--priority", "-p", choices=sorted(VALID_PRIORITIES))
    p_add.add_argument("--due", "-d")

    p_done = sub.add_parser("done", help="Mark a task done")
    p_done.add_argument("task_id")
    p_done.add_argument("--group", "-g", default="tasks")

    p_reopen = sub.add_parser("reopen", help="Reopen a task")
    p_reopen.add_argument("task_id")
    p_reopen.add_argument("--group", "-g", default="tasks")

    p_update = sub.add_parser("update", help="Update a task")
    p_update.add_argument("task_id")
    p_update.add_argument("--group", "-g", default="tasks")
    p_update.add_argument("--status", "-s")
    p_update.add_argument("--priority", "-p")
    p_update.add_argument("--due", "-d")

    p_archive = sub.add_parser("archive", help="Archive completed tasks")
    p_archive.add_argument("group", nargs="?", default="tasks")

    p_show = sub.add_parser("show", help="Show task details")
    p_show.add_argument("task_id")
    p_show.add_argument("--group", "-g", default="tasks")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd = args.command
    if cmd in ("list", "ls"):
        cmd_list(args)
    elif cmd == "add":
        cmd_add(args)
    elif cmd == "done":
        cmd_done(args)
    elif cmd == "reopen":
        cmd_reopen(args)
    elif cmd == "update":
        cmd_update(args)
    elif cmd == "archive":
        cmd_archive(args)
    elif cmd == "show":
        cmd_show(args)


if __name__ == "__main__":
    main()
