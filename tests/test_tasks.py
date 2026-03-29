"""Tests for the task management tool."""

import pytest
from pathlib import Path

from nanobot.agent.tools.tasks import TaskTool, _slugify, _parse_frontmatter_block


@pytest.fixture
def task_tool(tmp_path: Path) -> TaskTool:
    """Create a TaskTool with a temporary workspace."""
    return TaskTool(workspace=tmp_path)


@pytest.fixture
def populated_tool(task_tool: TaskTool, tmp_path: Path) -> TaskTool:
    """TaskTool with some pre-existing tasks."""
    tasks_dir = tmp_path / "memory" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "tasks.md").write_text(
        "---\ntitle: Buy groceries\nstatus: open\npriority: normal\n"
        "created: 2026-03-28\n---\n\nMilk, eggs, bread.\n\n"
        "---\ntitle: Fix leaky faucet\nstatus: done\npriority: low\n"
        "created: 2026-03-27\n---\n\n"
    )
    (tasks_dir / "refactor.md").write_text(
        "---\ntitle: Extract auth module\nstatus: done\npriority: high\n"
        "created: 2026-03-28\n---\n\n"
        "---\ntitle: Update tests\nstatus: done\npriority: normal\n"
        "created: 2026-03-28\n---\n\n"
    )
    return task_tool


# --- Unit tests ---


def test_slugify_basic():
    assert _slugify("Buy Groceries") == "buy-groceries"


def test_slugify_special_chars():
    assert _slugify("Fix the leak! (urgent)") == "fix-the-leak-urgent"


def test_slugify_long_title():
    slug = _slugify("a" * 100)
    assert len(slug) <= 80


def test_parse_frontmatter_block():
    text = "---\ntitle: Test\nstatus: open\n---\n\nBody text."
    task = _parse_frontmatter_block(text)
    assert task is not None
    assert task["title"] == "Test"
    assert task["status"] == "open"
    assert task["body"] == "Body text."


def test_parse_frontmatter_with_list():
    text = '---\ntitle: Test\ntags: ["a", "b"]\n---\n\n'
    task = _parse_frontmatter_block(text)
    assert task["tags"] == ["a", "b"]


def test_parse_frontmatter_invalid():
    assert _parse_frontmatter_block("no frontmatter here") is None


# --- Tool action tests ---


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
async def test_complete_not_found(task_tool: TaskTool):
    result = await task_tool.execute(action="complete", task_id="nonexistent")
    assert "not found" in result


@pytest.mark.asyncio
async def test_reopen_task(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Do laundry")
    await task_tool.execute(action="complete", task_id="laundry")
    result = await task_tool.execute(action="reopen", task_id="laundry")
    assert "open" in result


@pytest.mark.asyncio
async def test_update_task(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Fix bug")
    result = await task_tool.execute(
        action="update", task_id="fix-bug", status="in-progress", priority="high"
    )
    assert "Updated" in result


@pytest.mark.asyncio
async def test_project_group(task_tool: TaskTool):
    await task_tool.execute(
        action="create", group="migration", title="Step 1", priority="high"
    )
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
    # The done task is archived, the open one remains
    remaining = (tmp_path / "memory" / "tasks" / "tasks.md").read_text()
    assert "Buy groceries" in remaining
    assert "Fix leaky faucet" not in remaining


@pytest.mark.asyncio
async def test_task_with_due_and_tags(task_tool: TaskTool):
    await task_tool.execute(
        action="create",
        title="Review PR",
        due="2026-04-01",
        tags=["code-review", "urgent"],
    )
    result = await task_tool.execute(action="list")
    assert "Review PR" in result
    assert "due: 2026-04-01" in result


@pytest.mark.asyncio
async def test_find_by_substring(task_tool: TaskTool):
    await task_tool.execute(action="create", title="Implement user authentication")
    result = await task_tool.execute(action="complete", task_id="auth")
    assert "done" in result


@pytest.mark.asyncio
async def test_task_body(task_tool: TaskTool, tmp_path: Path):
    await task_tool.execute(
        action="create", title="Research options", body="Check A, B, and C."
    )
    content = (tmp_path / "memory" / "tasks" / "tasks.md").read_text()
    assert "Check A, B, and C." in content
