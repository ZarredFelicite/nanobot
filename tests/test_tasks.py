"""Tests for the task management tool (Dataview format)."""

import pytest
from pathlib import Path

from nanobot.agent.tools.tasks import (
    TaskTool,
    _parse_task_line,
    _render_task_line,
    _parse_task_file,
    _slugify,
)


# --- Parsing tests ---


def test_parse_simple_task():
    task = _parse_task_line("- [ ] Buy groceries")
    assert task is not None
    assert task["title"] == "Buy groceries"
    assert task["status"] == "todo"


def test_parse_done_task():
    task = _parse_task_line("- [x] Fix the bug  [completion:: 2026-03-28]")
    assert task["status"] == "done"
    assert task["completion"] == "2026-03-28"


def test_parse_in_progress():
    task = _parse_task_line("- [/] Working on it")
    assert task["status"] == "in-progress"


def test_parse_cancelled():
    task = _parse_task_line("- [-] Not doing this")
    assert task["status"] == "cancelled"


def test_parse_blocked():
    task = _parse_task_line("- [?] Waiting on response")
    assert task["status"] == "blocked"


def test_parse_with_fields():
    line = "- [ ] Deploy app  [priority:: high]  [due:: 2026-04-01]  [created:: 2026-03-30]"
    task = _parse_task_line(line)
    assert task["title"] == "Deploy app"
    assert task["priority"] == "high"
    assert task["due"] == "2026-04-01"
    assert task["created"] == "2026-03-30"


def test_parse_with_dependencies():
    line = "- [ ] Step 2  [dependsOn:: abc123]  [id:: def456]"
    task = _parse_task_line(line)
    assert task["dependsOn"] == "abc123"
    assert task["id"] == "def456"


def test_parse_non_task_line():
    assert _parse_task_line("# Heading") is None
    assert _parse_task_line("Just some text") is None
    assert _parse_task_line("") is None


def test_parse_asterisk_marker():
    task = _parse_task_line("* [ ] Asterisk task")
    assert task is not None
    assert task["title"] == "Asterisk task"


def test_parse_plus_marker():
    task = _parse_task_line("+ [ ] Plus task")
    assert task is not None
    assert task["title"] == "Plus task"


# --- Rendering tests ---


def test_render_simple():
    line = _render_task_line({"title": "Buy milk", "status": "todo"})
    assert line == "- [ ] Buy milk"


def test_render_done():
    line = _render_task_line({"title": "Done task", "status": "done", "completion": "2026-03-30"})
    assert line == "- [x] Done task  [completion:: 2026-03-30]"


def test_render_in_progress():
    line = _render_task_line({"title": "WIP", "status": "in-progress"})
    assert line == "- [/] WIP"


def test_render_with_fields():
    task = {
        "title": "Deploy",
        "status": "todo",
        "priority": "high",
        "due": "2026-04-01",
        "created": "2026-03-30",
    }
    line = _render_task_line(task)
    assert "- [ ] Deploy" in line
    assert "[priority:: high]" in line
    assert "[due:: 2026-04-01]" in line
    assert "[created:: 2026-03-30]" in line


def test_roundtrip():
    """Parse a line, render it, parse again — should be identical."""
    original = "- [ ] Test task  [priority:: high]  [created:: 2026-03-30]  [due:: 2026-04-01]"
    task = _parse_task_line(original)
    rendered = _render_task_line(task)
    reparsed = _parse_task_line(rendered)
    assert task["title"] == reparsed["title"]
    assert task["priority"] == reparsed["priority"]
    assert task["due"] == reparsed["due"]


# --- File parsing tests ---


def test_parse_file_with_header():
    text = "# My Tasks\n\n- [ ] Task one\n- [x] Task two  [completion:: 2026-03-28]\n"
    header, tasks = _parse_task_file(text)
    assert "# My Tasks" in header
    assert len(tasks) == 2
    assert tasks[0]["title"] == "Task one"
    assert tasks[1]["status"] == "done"


def test_parse_empty_file():
    header, tasks = _parse_task_file("")
    assert tasks == []


# --- Slugify tests ---


def test_slugify():
    assert _slugify("Buy Groceries") == "buy-groceries"
    assert _slugify("Fix the leak! (urgent)") == "fix-the-leak-urgent"
    assert len(_slugify("a" * 100)) <= 80


# --- Tool action tests ---


@pytest.fixture
def task_tool(tmp_path: Path) -> TaskTool:
    return TaskTool(workspace=tmp_path)


@pytest.fixture
def populated_tool(task_tool: TaskTool, tmp_path: Path) -> TaskTool:
    tasks_dir = tmp_path / "memory" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "tasks.md").write_text(
        "- [ ] Buy groceries  [created:: 2026-03-28]\n"
        "- [x] Fix leaky faucet  [priority:: low]  [created:: 2026-03-27]  [completion:: 2026-03-28]\n"
    )
    (tasks_dir / "refactor.md").write_text(
        "- [x] Extract auth module  [priority:: high]  [created:: 2026-03-28]  [completion:: 2026-03-29]\n"
        "- [x] Update tests  [created:: 2026-03-28]  [completion:: 2026-03-29]\n"
    )
    return task_tool


@pytest.mark.asyncio
async def test_create_task(task_tool: TaskTool):
    result = await task_tool.execute(action="create", title="Buy milk", priority="high")
    assert "Created task" in result
    assert "Buy milk" in result


@pytest.mark.asyncio
async def test_create_duplicate(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Buy milk")
    result = await task_tool.execute(action="create", title="Buy milk")
    assert "already exists" in result


@pytest.mark.asyncio
async def test_create_requires_title(task_tool: TaskTool):
    result = await task_tool.execute(action="create")
    assert "Error" in result


@pytest.mark.asyncio
async def test_create_writes_dataview_format(task_tool: TaskTool, tmp_path: Path):
    await task_tool.execute(action="create", title="Test task", priority="high", due="2026-04-01")
    content = (tmp_path / "memory" / "tasks" / "tasks.md").read_text()
    assert "- [ ] Test task" in content
    assert "[priority:: high]" in content
    assert "[due:: 2026-04-01]" in content
    assert "[created::" in content


@pytest.mark.asyncio
async def test_list_empty(task_tool: TaskTool):
    result = await task_tool.execute(action="list")
    assert "No tasks" in result


@pytest.mark.asyncio
async def test_list_tasks(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Task one")
    await task_tool.execute(action="create", title="Task two", priority="high")
    result = await task_tool.execute(action="list")
    assert "Task one" in result
    assert "Task two" in result


@pytest.mark.asyncio
async def test_list_all_groups(populated_tool: TaskTool):
    result = await populated_tool.execute(action="list", group="*")
    assert "tasks" in result
    assert "refactor" in result


@pytest.mark.asyncio
async def test_complete_task(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Do laundry")
    result = await task_tool.execute(action="complete", task_id="laundry")
    assert "done" in result


@pytest.mark.asyncio
async def test_complete_adds_completion_date(task_tool: TaskTool, tmp_path: Path):
    await task_tool.execute(action="create", title="Finish report")
    await task_tool.execute(action="complete", task_id="finish-report")
    content = (tmp_path / "memory" / "tasks" / "tasks.md").read_text()
    assert "[x]" in content
    assert "[completion::" in content


@pytest.mark.asyncio
async def test_complete_not_found(task_tool: TaskTool):
    result = await task_tool.execute(action="complete", task_id="nonexistent")
    assert "not found" in result


@pytest.mark.asyncio
async def test_reopen_task(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Do laundry")
    await task_tool.execute(action="complete", task_id="laundry")
    result = await task_tool.execute(action="reopen", task_id="laundry")
    assert "todo" in result


@pytest.mark.asyncio
async def test_reopen_removes_completion_date(task_tool: TaskTool, tmp_path: Path):
    await task_tool.execute(action="create", title="Reopen me")
    await task_tool.execute(action="complete", task_id="reopen-me")
    await task_tool.execute(action="reopen", task_id="reopen-me")
    content = (tmp_path / "memory" / "tasks" / "tasks.md").read_text()
    assert "[ ]" in content
    assert "[completion::" not in content


@pytest.mark.asyncio
async def test_update_task(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Fix bug")
    result = await task_tool.execute(
        action="update", task_id="fix-bug", status="in-progress", priority="high"
    )
    assert "Updated" in result


@pytest.mark.asyncio
async def test_project_group(task_tool: TaskTool):
    await task_tool.execute(action="create", group="migration", title="Step 1", priority="high")
    await task_tool.execute(action="create", group="migration", title="Step 2")
    result = await task_tool.execute(action="list", group="migration")
    assert "Step 1" in result
    assert "Step 2" in result


@pytest.mark.asyncio
async def test_all_done_hint(task_tool: TaskTool):
    await task_tool.execute(action="create", group="proj", title="Only task")
    result = await task_tool.execute(action="complete", group="proj", task_id="only-task")
    assert "archive" in result.lower()


@pytest.mark.asyncio
async def test_archive_project(populated_tool: TaskTool, tmp_path: Path):
    result = await populated_tool.execute(action="archive", group="refactor")
    assert "Archived" in result
    assert (tmp_path / "memory" / "tasks" / "archive" / "refactor.md").exists()
    assert not (tmp_path / "memory" / "tasks" / "refactor.md").exists()


@pytest.mark.asyncio
async def test_archive_general_tasks(populated_tool: TaskTool, tmp_path: Path):
    result = await populated_tool.execute(action="archive", group="tasks")
    assert "Archived 1" in result
    remaining = (tmp_path / "memory" / "tasks" / "tasks.md").read_text()
    assert "Buy groceries" in remaining
    assert "Fix leaky faucet" not in remaining


@pytest.mark.asyncio
async def test_task_with_due(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Review PR", due="2026-04-01")
    result = await task_tool.execute(action="list")
    assert "Review PR" in result
    assert "due: 2026-04-01" in result


@pytest.mark.asyncio
async def test_find_by_substring(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Implement user authentication")
    result = await task_tool.execute(action="complete", task_id="auth")
    assert "done" in result
