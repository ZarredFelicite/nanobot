"""Task management tool for persistent todo tracking."""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


def _slugify(title: str) -> str:
    """Convert a title to a filename-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80] or "untitled"


def _parse_task(path: Path) -> dict[str, Any] | None:
    """Parse a task markdown file into a dict."""
    if not path.exists() or not path.suffix == ".md":
        return None
    text = path.read_text()
    # Parse YAML frontmatter
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not m:
        return None
    frontmatter, body = m.group(1), m.group(2).strip()
    task: dict[str, Any] = {"_file": path.name, "_path": str(path)}
    for line in frontmatter.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        # Handle YAML lists
        if val == "":
            continue
        if val.startswith("["):
            items = re.findall(r'"([^"]*)"', val)
            task[key] = items
        elif val in ("true", "false"):
            task[key] = val == "true"
        else:
            task[key] = val.strip('"').strip("'")
    task["body"] = body
    return task


def _render_task(task: dict[str, Any]) -> str:
    """Render a task dict back to markdown with YAML frontmatter."""
    fields = {}
    for k, v in task.items():
        if k.startswith("_") or k == "body":
            continue
        fields[k] = v

    lines = ["---"]
    for k, v in fields.items():
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


def _task_summary(task: dict[str, Any]) -> str:
    """One-line summary of a task."""
    status = task.get("status", "open")
    prio = task.get("priority", "normal")
    title = task.get("title", task.get("_file", "?"))
    due = task.get("due", "")
    due_str = f" (due: {due})" if due else ""
    return f"[{status}] {title} (priority: {prio}){due_str}"


class TaskTool(Tool):
    """Manage persistent tasks as markdown notes."""

    name = "tasks"
    parallel_safe = False
    description = (
        "Manage persistent tasks stored as markdown notes. Actions: list, create, "
        "update, complete, archive. Use for tracking user todos and agent work progress. "
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
                "description": "Task title (for create)",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "description": "Task priority (for create/update, default: normal)",
            },
            "due": {
                "type": "string",
                "description": "Due date in YYYY-MM-DD format (for create/update)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for the task (for create/update)",
            },
            "body": {
                "type": "string",
                "description": "Task details/notes body text (for create/update)",
            },
            "task_id": {
                "type": "string",
                "description": (
                    "Task identifier — the slug portion of the task entry within "
                    "a group file, or just a substring to match. Required for "
                    "update/complete/reopen."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["open", "in-progress", "done", "blocked"],
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
        """Get the path for a task group file."""
        safe = re.sub(r"[^\w-]", "", group.lower())
        return self._tasks_dir / f"{safe}.md"

    def _load_group(self, group: str) -> list[dict[str, Any]]:
        """Load all tasks from a group file."""
        path = self._group_path(group)
        if not path.exists():
            return []
        text = path.read_text()
        return _parse_multi_task_file(text)

    def _save_group(self, group: str, tasks: list[dict[str, Any]]) -> None:
        """Save tasks to a group file. Delete file if empty."""
        path = self._group_path(group)
        if not tasks:
            if path.exists():
                path.unlink()
            return
        blocks = []
        for task in tasks:
            blocks.append(_render_task(task))
        path.write_text("\n".join(blocks))

    def _find_task(
        self, tasks: list[dict[str, Any]], task_id: str
    ) -> tuple[int, dict[str, Any]] | None:
        """Find a task by slug or title substring."""
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
            return self._set_status(group, kwargs.get("task_id"), "open")
        elif action == "archive":
            return self._archive_group(group)
        return f"Unknown action: {action}"

    def _list_tasks(self, group: str, status_filter: str | None = None) -> str:
        """List tasks, optionally filtered by status."""
        if group == "*":
            # List all groups
            files = sorted(self._tasks_dir.glob("*.md"))
            if not files:
                return "No task groups found."
            lines = ["Task groups:"]
            for f in files:
                tasks = self._load_group(f.stem)
                open_count = sum(1 for t in tasks if t.get("status") != "done")
                done_count = sum(1 for t in tasks if t.get("status") == "done")
                lines.append(f"  {f.stem}.md — {open_count} open, {done_count} done")
            # Also check archive
            archived = sorted(self._archive_dir.glob("*.md"))
            if archived:
                lines.append(f"\nArchived: {len(archived)} groups")
            return "\n".join(lines)

        tasks = self._load_group(group)
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
        tasks = self._load_group(group)
        # Check for duplicate
        slug = _slugify(title)
        for t in tasks:
            if _slugify(t.get("title", "")) == slug:
                return f"Error: task '{title}' already exists in '{group}'"
        task: dict[str, Any] = {
            "title": title,
            "status": "open",
            "priority": kwargs.get("priority", "normal"),
            "created": str(date.today()),
        }
        if kwargs.get("due"):
            task["due"] = kwargs["due"]
        if kwargs.get("tags"):
            task["tags"] = kwargs["tags"]
        task["body"] = kwargs.get("body", "")
        tasks.append(task)
        self._save_group(group, tasks)
        return f"Created task '{title}' in group '{group}'"

    def _update_task(self, group: str, kwargs: dict[str, Any]) -> str:
        task_id = kwargs.get("task_id")
        if not task_id:
            return "Error: task_id is required for update"
        tasks = self._load_group(group)
        result = self._find_task(tasks, task_id)
        if not result:
            return f"Error: task '{task_id}' not found in '{group}'"
        idx, task = result
        if kwargs.get("status"):
            task["status"] = kwargs["status"]
        if kwargs.get("priority"):
            task["priority"] = kwargs["priority"]
        if kwargs.get("due"):
            task["due"] = kwargs["due"]
        if kwargs.get("title"):
            task["title"] = kwargs["title"]
        if kwargs.get("body") is not None:
            task["body"] = kwargs["body"]
        if kwargs.get("tags"):
            task["tags"] = kwargs["tags"]
        tasks[idx] = task
        self._save_group(group, tasks)
        return f"Updated task '{task.get('title')}' in '{group}'"

    def _set_status(self, group: str, task_id: str | None, status: str) -> str:
        if not task_id:
            return "Error: task_id is required"
        tasks = self._load_group(group)
        result = self._find_task(tasks, task_id)
        if not result:
            return f"Error: task '{task_id}' not found in '{group}'"
        idx, task = result
        task["status"] = status
        tasks[idx] = task
        self._save_group(group, tasks)
        # Check if all tasks in a project group are done (auto-archive hint)
        if group != "tasks" and all(t.get("status") == "done" for t in tasks):
            return (
                f"Marked '{task.get('title')}' as {status} in '{group}'. "
                f"All tasks in '{group}' are now done — consider archiving with "
                f"action='archive'."
            )
        return f"Marked '{task.get('title')}' as {status} in '{group}'"

    def _archive_group(self, group: str) -> str:
        """Move a completed project group file to the archive directory."""
        if group == "tasks":
            # For the general tasks file, archive only done tasks
            tasks = self._load_group(group)
            done = [t for t in tasks if t.get("status") == "done"]
            remaining = [t for t in tasks if t.get("status") != "done"]
            if not done:
                return "No completed tasks to archive in 'tasks'."
            # Append done tasks to archive
            archive_path = self._archive_dir / f"tasks-{date.today()}.md"
            existing = archive_path.read_text() if archive_path.exists() else ""
            blocks = [_render_task(t) for t in done]
            archive_path.write_text(existing + "\n".join(blocks))
            self._save_group(group, remaining)
            return f"Archived {len(done)} completed tasks from 'tasks'."

        src = self._group_path(group)
        if not src.exists():
            return f"Error: group '{group}' not found"
        dst = self._archive_dir / src.name
        shutil.move(str(src), str(dst))
        return f"Archived group '{group}' to {dst}"


def _parse_multi_task_file(text: str) -> list[dict[str, Any]]:
    """Parse a file containing multiple ---frontmatter---body blocks."""
    tasks = []
    # Find all frontmatter blocks: --- ... ---
    for m in re.finditer(r"^---\n(.*?)\n---\n?(.*?)(?=\n---\n|\Z)", text, re.DOTALL | re.MULTILINE):
        frontmatter, body = m.group(1), m.group(2).strip()
        # Skip if this looks like a closing --- matched as opening
        if not any(":" in line for line in frontmatter.split("\n")):
            continue
        task = _parse_yaml_block(frontmatter)
        task["body"] = body
        tasks.append(task)
    return tasks


def _parse_frontmatter_block(text: str) -> dict[str, Any] | None:
    """Parse a single ---frontmatter---body block."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not m:
        return None
    task = _parse_yaml_block(m.group(1))
    task["body"] = m.group(2).strip()
    return task


def _parse_yaml_block(frontmatter: str) -> dict[str, Any]:
    """Parse YAML-like frontmatter lines into a dict."""
    task: dict[str, Any] = {}
    for line in frontmatter.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if val == "":
            continue
        if val.startswith("["):
            items = re.findall(r'"([^"]*)"', val)
            task[key] = items
        elif val in ("true", "false"):
            task[key] = val == "true"
        else:
            task[key] = val.strip('"').strip("'")
    return task
