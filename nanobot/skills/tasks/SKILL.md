---
name: tasks
description: Persistent task management with markdown notes — user todos and agent work tracking.
always: true
---

# Task Management

You have a built-in `tasks` tool for managing persistent tasks stored as markdown notes in `memory/tasks/`.

## Task Format

Each task is stored as a YAML frontmatter block within a group file:

```markdown
---
title: Implement user authentication
status: open
priority: high
created: 2026-03-29
due: 2026-04-05
tags: ["backend", "security"]
---

Additional notes, context, or subtask details go here.
Links to relevant files, decisions, or references.
```

### Fields

| Field | Values | Required |
|-------|--------|----------|
| `title` | Free text | Yes |
| `status` | `open`, `in-progress`, `done`, `blocked` | Yes (default: open) |
| `priority` | `low`, `normal`, `high` | Yes (default: normal) |
| `created` | `YYYY-MM-DD` | Auto-set |
| `due` | `YYYY-MM-DD` | Optional |
| `tags` | List of strings | Optional |

## Task Groups

Tasks are organized into **group files** within `memory/tasks/`:

### General Tasks (`tasks.md`)
- User-requested todos — things to remember or do in the future.
- Created when the user says "remind me to...", "add a task for...", "I need to...", etc.
- Persist indefinitely until completed or archived.
- Completed tasks can be archived (moved to `archive/tasks-YYYY-MM-DD.md`).

### Project Groups (e.g., `refactor.md`, `migration.md`)
- Agent-created task files for tracking multi-step work.
- **Create proactively** when starting complex, multi-step tasks.
- Name the group after the work being done (e.g., `auth-rewrite`, `api-migration`).
- **Archive automatically** when all tasks in the group are done — moves to `archive/`.
- Users can monitor progress by asking to list tasks.

## When to Use Tasks

### As the agent, create project tasks when:
- Starting work that involves 3+ distinct steps
- The user requests a complex feature or refactor
- You need to track what's done vs remaining across tool calls

### Create general tasks when the user:
- Asks to be reminded of something
- Mentions something they need to do later
- Says "add a task", "todo", "remember to..."

## Tool Actions

### `list` — View tasks
- `group: "*"` — List all task groups with counts
- `group: "tasks"` — List general tasks (default)
- `group: "refactor"` — List a specific project's tasks

### `create` — Add a task
- Requires `title`, optional `priority`, `due`, `tags`, `body`
- Specify `group` for project tasks (default: "tasks")

### `update` — Modify a task
- Use `task_id` (title substring or slug) to identify the task
- Can update `status`, `priority`, `due`, `title`, `body`, `tags`

### `complete` — Mark a task done
- Shorthand for setting status to "done"
- For project groups: hints when all tasks are done (archive prompt)

### `reopen` — Mark a completed task as open again

### `archive` — Archive completed work
- For `tasks`: moves completed tasks to dated archive file
- For project groups: moves entire file to `archive/` directory

## Directory Structure

```
memory/tasks/
├── tasks.md              # General user todos
├── auth-rewrite.md       # Active project tracking
├── api-migration.md      # Active project tracking
└── archive/
    ├── tasks-2026-03-28.md   # Archived general tasks
    └── refactor.md           # Completed project archive
```

## Examples

When the user says "remind me to update the SSL certs next week":
```
tasks(action="create", group="tasks", title="Update SSL certificates", due="2026-04-05", priority="high")
```

When starting a complex refactor:
```
tasks(action="create", group="auth-rewrite", title="Extract auth middleware", priority="high", body="Move from monolithic handler to standalone middleware module")
tasks(action="create", group="auth-rewrite", title="Add JWT validation", body="Replace session tokens with JWT")
tasks(action="create", group="auth-rewrite", title="Update tests for new auth flow")
tasks(action="create", group="auth-rewrite", title="Update API documentation")
```

As work progresses:
```
tasks(action="update", group="auth-rewrite", task_id="extract-auth", status="in-progress")
tasks(action="complete", group="auth-rewrite", task_id="extract-auth")
```

When all project tasks are done:
```
tasks(action="archive", group="auth-rewrite")
```
