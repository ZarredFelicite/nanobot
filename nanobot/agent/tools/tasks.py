"""Task management tool using Obsidian Tasks Dataview format.

Tasks are stored as single-line markdown checkboxes with inline fields:
  - [ ] Task description  [due:: 2026-04-01]  [priority:: high]  [created:: 2026-03-30]
  - [x] Completed task  [completion:: 2026-03-30]
  - [/] In-progress task
  - [-] Cancelled task
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool

# Checkbox status symbols -> status names
_STATUS_SYMBOLS = {
    " ": "todo",
    "x": "done",
    "/": "in-progress",
    "-": "cancelled",
    "?": "blocked",
}
_STATUS_TO_SYMBOL = {v: k for k, v in _STATUS_SYMBOLS.items()}

# Regex to parse a task line: - [x] description  [field:: value]  [field:: value]
_TASK_RE = re.compile(r"^[-*+]\s+\[(.)\]\s+(.+)$")
# Regex to extract inline fields: [field:: value]
_FIELD_RE = re.compile(r"\[(\w+)::\s*([^\]]*)\]")


def _parse_task_line(line: str) -> dict[str, Any] | None:
    """Parse a single task line into a dict."""
    m = _TASK_RE.match(line.strip())
    if not m:
        return None
    symbol, rest = m.group(1), m.group(2)
    status = _STATUS_SYMBOLS.get(symbol, "todo")

    # Extract inline fields
    fields: dict[str, str] = {}
    for fm in _FIELD_RE.finditer(rest):
        fields[fm.group(1)] = fm.group(2).strip()

    # Description is everything before the first inline field
    desc = _FIELD_RE.sub("", rest).strip()

    task: dict[str, Any] = {"title": desc, "status": status}
    task.update(fields)
    return task


def _render_task_line(task: dict[str, Any]) -> str:
    """Render a task dict as a single markdown checkbox line."""
    symbol = _STATUS_TO_SYMBOL.get(task.get("status", "todo"), " ")
    title = task.get("title", "untitled")

    # Build inline fields (skip title and status, they're in the checkbox)
    fields = []
    # Ordered: priority, created, scheduled, start, due, completion, cancelled
    field_order = [
        "priority", "created", "scheduled", "start", "due",
        "completion", "cancelled", "repeat", "id", "dependsOn",
    ]
    seen = set()
    for key in field_order:
        if key in task:
            fields.append(f"[{key}:: {task[key]}]")
            seen.add(key)
    # Any remaining fields not in the standard order
    for key, val in task.items():
        if key not in ("title", "status") and key not in seen:
            fields.append(f"[{key}:: {val}]")

    suffix = "  " + "  ".join(fields) if fields else ""
    return f"- [{symbol}] {title}{suffix}"


def _parse_task_file(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Parse a task file, returning non-task lines and task dicts.

    Returns (header_lines, tasks) where header_lines are any non-task
    lines at the top of the file (headings, blank lines, comments).
    """
    header: list[str] = []
    tasks: list[dict[str, Any]] = []
    in_header = True

    for line in text.split("\n"):
        task = _parse_task_line(line)
        if task:
            in_header = False
            tasks.append(task)
        elif in_header:
            header.append(line)
        # Skip non-task lines after tasks start (stale comments etc.)

    return header, tasks


def _render_task_file(header: list[str], tasks: list[dict[str, Any]]) -> str:
    """Render header lines and tasks back to a file."""
    lines = list(header)
    # Ensure header ends with blank line before tasks
    if lines and lines[-1].strip():
        lines.append("")
    for task in tasks:
        lines.append(_render_task_line(task))
    return "\n".join(lines) + "\n"


def _slugify(title: str) -> str:
    """Convert a title to a slug for matching."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")[:80] or "untitled"


def _task_summary(task: dict[str, Any]) -> str:
    """One-line summary of a task for display."""
    status = task.get("status", "todo")
    prio = task.get("priority", "")
    title = task.get("title", "?")
    due = task.get("due", "")
    parts = [f"[{status}] {title}"]
    if prio:
        parts.append(f"(priority: {prio})")
    if due:
        parts.append(f"(due: {due})")
    return " ".join(parts)


class TaskTool(Tool):
    """Manage persistent tasks in Obsidian Tasks Dataview format."""

    name = "tasks"
    parallel_safe = False
    description = (
        "Manage persistent tasks stored as markdown checkboxes with Dataview inline fields. "
        "Actions: list, create, update, complete, reopen, archive. "
        "Tasks in 'tasks.md' group are general user todos. Other groups (e.g. 'refactor', "
        "'migration') are project task files that track multi-step agent work — create these "
        "proactively when starting complex work, and archive when all tasks are done."
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "complete", "reopen", "archive"],
                "description": "Action to perform",
            },
            "group": {
                "type": "string",
                "description": (
                    "Task group/file name (without .md). 'tasks' for general user "
                    "todos, or a project name like 'refactor' for agent work tracking. "
                    "Defaults to 'tasks'."
                ),
            },
            "title": {
                "type": "string",
                "description": "Task description text (for create)",
            },
            "priority": {
                "type": "string",
                "enum": ["lowest", "low", "medium", "high", "highest"],
                "description": "Task priority (for create/update). Omit for normal priority.",
            },
            "due": {
                "type": "string",
                "description": "Due date in YYYY-MM-DD format (for create/update)",
            },
            "scheduled": {
                "type": "string",
                "description": "Scheduled date in YYYY-MM-DD format (for create/update)",
            },
            "start": {
                "type": "string",
                "description": "Start date in YYYY-MM-DD format (for create/update)",
            },
            "task_id": {
                "type": "string",
                "description": (
                    "Task identifier — a substring of the task title to match. "
                    "Required for update/complete/reopen."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["todo", "in-progress", "done", "cancelled", "blocked"],
                "description": "New status (for update)",
            },
        },
        "required": ["action"],
    }

    def __init__(self, workspace: Path):
        self._tasks_dir = workspace / "memory" / "tasks"
        self._archive_dir = self._tasks_dir / "archive"

    def _ensure_dirs(self) -> None:
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    def _group_path(self, group: str) -> Path:
        safe = re.sub(r"[^\w-]", "", group.lower())
        return self._tasks_dir / f"{safe}.md"

    def _load_group(self, group: str) -> tuple[list[str], list[dict[str, Any]]]:
        """Load header and tasks from a group file."""
        path = self._group_path(group)
        if not path.exists():
            return [], []
        return _parse_task_file(path.read_text())

    def _save_group(
        self, group: str, tasks: list[dict[str, Any]], header: list[str] | None = None
    ) -> None:
        path = self._group_path(group)
        if not tasks:
            if path.exists():
                path.unlink()
            return
        if header is None:
            header = []
        path.write_text(_render_task_file(header, tasks))

    def _find_task(
        self, tasks: list[dict[str, Any]], task_id: str
    ) -> tuple[int, dict[str, Any]] | None:
        tid = task_id.lower()
        for i, t in enumerate(tasks):
            slug = _slugify(t.get("title", ""))
            title = t.get("title", "").lower()
            if slug == tid or tid in title or tid in slug:
                return i, t
        return None

    async def execute(self, **kwargs: Any) -> str:
        self._ensure_dirs()
        action = kwargs.get("action")
        group = kwargs.get("group", "tasks")

        if action == "list":
            return self._list_tasks(group, kwargs.get("status"))
        elif action == "create":
            return self._create_task(group, kwargs)
        elif action == "update":
            return self._update_task(group, kwargs)
        elif action == "complete":
            return self._set_status(group, kwargs.get("task_id"), "done")
        elif action == "reopen":
            return self._set_status(group, kwargs.get("task_id"), "todo")
        elif action == "archive":
            return self._archive_group(group)
        return f"Unknown action: {action}"

    def _list_tasks(self, group: str, status_filter: str | None = None) -> str:
        if group == "*":
            files = sorted(self._tasks_dir.glob("*.md"))
            if not files:
                return "No task groups found."
            lines = ["Task groups:"]
            for f in files:
                _, tasks = self._load_group(f.stem)
                open_count = sum(1 for t in tasks if t.get("status") != "done")
                done_count = sum(1 for t in tasks if t.get("status") == "done")
                lines.append(f"  {f.stem}.md — {open_count} open, {done_count} done")
            archived = sorted(self._archive_dir.glob("*.md"))
            if archived:
                lines.append(f"\nArchived: {len(archived)} groups")
            return "\n".join(lines)

        _, tasks = self._load_group(group)
        if not tasks:
            return f"No tasks in group '{group}'."
        if status_filter:
            tasks = [t for t in tasks if t.get("status") == status_filter]
        if not tasks:
            return f"No tasks matching status '{status_filter}' in '{group}'."
        lines = [f"Tasks in '{group}':"]
        for t in tasks:
            lines.append(f"  • {_task_summary(t)}")
        return "\n".join(lines)

    def _create_task(self, group: str, kwargs: dict[str, Any]) -> str:
        title = kwargs.get("title")
        if not title:
            return "Error: title is required for create"
        header, tasks = self._load_group(group)
        slug = _slugify(title)
        for t in tasks:
            if _slugify(t.get("title", "")) == slug:
                return f"Error: task '{title}' already exists in '{group}'"
        task: dict[str, Any] = {
            "title": title,
            "status": "todo",
            "created": str(date.today()),
        }
        if kwargs.get("priority"):
            task["priority"] = kwargs["priority"]
        if kwargs.get("due"):
            task["due"] = kwargs["due"]
        if kwargs.get("scheduled"):
            task["scheduled"] = kwargs["scheduled"]
        if kwargs.get("start"):
            task["start"] = kwargs["start"]
        tasks.append(task)
        self._save_group(group, tasks, header)
        return f"Created task '{title}' in group '{group}'"

    def _update_task(self, group: str, kwargs: dict[str, Any]) -> str:
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: task_id is required for update"
        header, tasks = self._load_group(group)
        result = self._find_task(tasks, task_id)
        if not result:
            return f"Error: task '{task_id}' not found in '{group}'"
        idx, task = result
        if kwargs.get("status"):
            task["status"] = kwargs["status"]
            if kwargs["status"] == "done" and "completion" not in task:
                task["completion"] = str(date.today())
        if kwargs.get("priority"):
            task["priority"] = kwargs["priority"]
        if kwargs.get("due"):
            task["due"] = kwargs["due"]
        if kwargs.get("scheduled"):
            task["scheduled"] = kwargs["scheduled"]
        if kwargs.get("start"):
            task["start"] = kwargs["start"]
        if kwargs.get("title"):
            task["title"] = kwargs["title"]
        tasks[idx] = task
        self._save_group(group, tasks, header)
        return f"Updated task '{task.get('title')}' in '{group}'"

    def _set_status(self, group: str, task_id: str | None, status: str) -> str:
        if not task_id:
            return "Error: task_id is required"
        header, tasks = self._load_group(group)
        result = self._find_task(tasks, task_id)
        if not result:
            return f"Error: task '{task_id}' not found in '{group}'"
        idx, task = result
        task["status"] = status
        if status == "done":
            task["completion"] = str(date.today())
        elif "completion" in task:
            del task["completion"]
        tasks[idx] = task
        self._save_group(group, tasks, header)
        if group != "tasks" and all(t.get("status") == "done" for t in tasks):
            return (
                f"Marked '{task.get('title')}' as {status} in '{group}'. "
                f"All tasks in '{group}' are now done — consider archiving with "
                f"action='archive'."
            )
        return f"Marked '{task.get('title')}' as {status} in '{group}'"

    def _archive_group(self, group: str) -> str:
        if group == "tasks":
            header, tasks = self._load_group(group)
            done = [t for t in tasks if t.get("status") == "done"]
            remaining = [t for t in tasks if t.get("status") != "done"]
            if not done:
                return "No completed tasks to archive in 'tasks'."
            archive_path = self._archive_dir / f"tasks-{date.today()}.md"
            existing = archive_path.read_text() if archive_path.exists() else ""
            archive_path.write_text(
                existing + _render_task_file([], done)
            )
            self._save_group(group, remaining, header)
            return f"Archived {len(done)} completed tasks from 'tasks'."

        src = self._group_path(group)
        if not src.exists():
            return f"Error: group '{group}' not found"
        dst = self._archive_dir / src.name
        shutil.move(str(src), str(dst))
        return f"Archived group '{group}' to {dst}"
