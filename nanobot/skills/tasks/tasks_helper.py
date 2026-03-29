#!/usr/bin/env python3
"""CLI helper for nanobot task management.

Standalone script to view and manage tasks from the command line,
independent of the nanobot agent. Operates on the same markdown task
files in ~/.nanobot/workspace/memory/tasks/.

Usage:
    tasks_helper.py list [GROUP]        List tasks (GROUP defaults to 'tasks', '*' for all)
    tasks_helper.py add TITLE [--group GROUP] [--priority PRIO] [--due DATE] [--tags t1,t2]
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

VALID_STATUSES = {"open", "in-progress", "done", "blocked"}
VALID_PRIORITIES = {"low", "normal", "high"}

# ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_MAGENTA = "\033[35m"

STATUS_COLORS = {
    "open": C_CYAN,
    "in-progress": C_YELLOW,
    "done": C_GREEN,
    "blocked": C_RED,
}

PRIO_COLORS = {
    "high": C_RED,
    "normal": "",
    "low": C_DIM,
}


def slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")[:80] or "untitled"


def parse_yaml_block(frontmatter: str) -> dict:
    task = {}
    for line in frontmatter.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if not val:
            continue
        if val.startswith("["):
            task[key] = re.findall(r'"([^"]*)"', val)
        elif val in ("true", "false"):
            task[key] = val == "true"
        else:
            task[key] = val.strip('"').strip("'")
    return task


def parse_group(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text()
    tasks = []
    for m in re.finditer(
        r"^---\n(.*?)\n---\n?(.*?)(?=\n---\n|\Z)", text, re.DOTALL | re.MULTILINE
    ):
        frontmatter, body = m.group(1), m.group(2).strip()
        if not any(":" in line for line in frontmatter.split("\n")):
            continue
        task = parse_yaml_block(frontmatter)
        task["body"] = body
        tasks.append(task)
    return tasks


def render_task(task: dict) -> str:
    lines = ["---"]
    for k, v in task.items():
        if k in ("body",):
            continue
        if isinstance(v, list):
            items = ", ".join(f'"{i}"' for i in v)
            lines.append(f"{k}: [{items}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if task.get("body"):
        lines.append(task["body"])
    return "\n".join(lines) + "\n"


def save_group(group: str, tasks: list[dict]) -> None:
    path = TASKS_DIR / f"{group}.md"
    if not tasks:
        if path.exists():
            path.unlink()
        return
    path.write_text("\n".join(render_task(t) for t in tasks))


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
            tasks = parse_group(f)
            open_c = sum(1 for t in tasks if t.get("status") != "done")
            done_c = sum(1 for t in tasks if t.get("status") == "done")
            print(f"  {C_CYAN}{f.stem}{C_RESET}  {open_c} open, {done_c} done")
        archived = list(ARCHIVE_DIR.glob("*.md"))
        if archived:
            print(f"\n  {C_DIM}Archived: {len(archived)} groups{C_RESET}")
        return

    tasks = parse_group(group_path(group))
    if not tasks:
        print(f"No tasks in '{group}'.")
        return

    print(f"{C_BOLD}Tasks in '{group}':{C_RESET}")
    for t in tasks:
        status = t.get("status", "open")
        prio = t.get("priority", "normal")
        title = t.get("title", "?")
        due = t.get("due", "")
        due_str = f"  {C_DIM}due: {due}{C_RESET}" if due else ""
        slug = slugify(title)
        print(f"  [{fmt_status(status)}] {title}  {C_DIM}({fmt_prio(prio)}){C_RESET}{due_str}  {C_DIM}id: {slug}{C_RESET}")


def cmd_add(args: argparse.Namespace) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    group = args.group or "tasks"
    tasks = parse_group(group_path(group))
    slug = slugify(args.title)
    for t in tasks:
        if slugify(t.get("title", "")) == slug:
            print(f"Error: task '{args.title}' already exists in '{group}'", file=sys.stderr)
            sys.exit(1)
    task = {
        "title": args.title,
        "status": "open",
        "priority": args.priority or "normal",
        "created": str(date.today()),
    }
    if args.due:
        task["due"] = args.due
    if args.tags:
        task["tags"] = [t.strip() for t in args.tags.split(",")]
    task["body"] = ""
    tasks.append(task)
    save_group(group, tasks)
    print(f"Created '{args.title}' in '{group}'  {C_DIM}(id: {slug}){C_RESET}")


def cmd_done(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    tasks = parse_group(group_path(group))
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    idx, task = result
    task["status"] = "done"
    tasks[idx] = task
    save_group(group, tasks)
    print(f"{C_GREEN}Completed:{C_RESET} {task.get('title')}")
    if group != "tasks" and all(t.get("status") == "done" for t in tasks):
        print(f"{C_YELLOW}All tasks in '{group}' done. Run: tasks_helper.py archive {group}{C_RESET}")


def cmd_reopen(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    tasks = parse_group(group_path(group))
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    idx, task = result
    task["status"] = "open"
    tasks[idx] = task
    save_group(group, tasks)
    print(f"Reopened: {task.get('title')}")


def cmd_update(args: argparse.Namespace) -> None:
    group = args.group or "tasks"
    tasks = parse_group(group_path(group))
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
    if args.priority:
        if args.priority not in VALID_PRIORITIES:
            print(f"Error: invalid priority '{args.priority}'", file=sys.stderr)
            sys.exit(1)
        task["priority"] = args.priority
    if args.due:
        task["due"] = args.due
    tasks[idx] = task
    save_group(group, tasks)
    print(f"Updated: {task.get('title')}")


def cmd_archive(args: argparse.Namespace) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    group = args.group or "tasks"

    if group == "tasks":
        tasks = parse_group(group_path(group))
        done = [t for t in tasks if t.get("status") == "done"]
        remaining = [t for t in tasks if t.get("status") != "done"]
        if not done:
            print("No completed tasks to archive.")
            return
        archive_path = ARCHIVE_DIR / f"tasks-{date.today()}.md"
        existing = archive_path.read_text() if archive_path.exists() else ""
        archive_path.write_text(existing + "\n".join(render_task(t) for t in done))
        save_group(group, remaining)
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
    tasks = parse_group(group_path(group))
    result = find_task(tasks, args.task_id)
    if not result:
        print(f"Error: task '{args.task_id}' not found in '{group}'", file=sys.stderr)
        sys.exit(1)
    _, task = result
    print(f"{C_BOLD}{task.get('title', '?')}{C_RESET}")
    print(f"  Status:   {fmt_status(task.get('status', 'open'))}")
    print(f"  Priority: {fmt_prio(task.get('priority', 'normal'))}")
    print(f"  Created:  {task.get('created', '?')}")
    if task.get("due"):
        print(f"  Due:      {task['due']}")
    if task.get("tags"):
        print(f"  Tags:     {', '.join(task['tags'])}")
    if task.get("body"):
        print(f"\n{task['body']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Nanobot task manager")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", aliases=["ls"], help="List tasks")
    p_list.add_argument("group", nargs="?", default="tasks")

    p_add = sub.add_parser("add", help="Add a task")
    p_add.add_argument("title")
    p_add.add_argument("--group", "-g", default="tasks")
    p_add.add_argument("--priority", "-p", choices=["low", "normal", "high"])
    p_add.add_argument("--due", "-d")
    p_add.add_argument("--tags", "-t")

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
