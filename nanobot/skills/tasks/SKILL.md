---
name: tasks
description: Persistent task management using Obsidian Tasks Dataview format — user todos and agent work tracking.
always: true
---

# Task Management

You have a built-in `tasks` tool for managing persistent tasks stored as markdown checkboxes with Dataview inline fields in `memory/tasks/`.

## Task Format

Tasks use the Obsidian Tasks Dataview format — single-line markdown checkboxes with `[field:: value]` inline fields:

```markdown
- [ ] Buy groceries  [priority:: high]  [due:: 2026-04-01]  [created:: 2026-03-30]
- [x] Fix leaky faucet  [priority:: low]  [created:: 2026-03-27]  [completion:: 2026-03-28]
- [/] Refactor auth module  [priority:: high]  [due:: 2026-04-01]
- [-] Cancelled task  [cancelled:: 2026-03-29]
- [?] Waiting on response  [due:: 2026-04-05]
```

### Status Symbols

| Symbol | Status | Description |
|--------|--------|-------------|
| `[ ]` | `todo` | Not started |
| `[/]` | `in-progress` | Currently working on |
| `[x]` | `done` | Completed |
| `[-]` | `cancelled` | Won't do |
| `[?]` | `blocked` | Waiting on something |

### Inline Fields

All fields use `[fieldName:: value]` syntax, separated by two spaces:

| Field | Format | Notes |
|-------|--------|-------|
| `priority` | `lowest`, `low`, `medium`, `high`, `highest` | Omit for normal priority |
| `created` | `YYYY-MM-DD` | Auto-set on creation |
| `due` | `YYYY-MM-DD` | Deadline |
| `scheduled` | `YYYY-MM-DD` | When to work on it |
| `start` | `YYYY-MM-DD` | Cannot start before this |
| `completion` | `YYYY-MM-DD` | Auto-set when completed |
| `cancelled` | `YYYY-MM-DD` | Auto-set when cancelled |
| `repeat` | Natural language | e.g. `every week on Monday` |
| `id` | Alphanumeric | Unique task identifier |
| `dependsOn` | Comma-separated IDs | Task dependencies |

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
- Requires `title`, optional `priority`, `due`, `scheduled`, `start`
- Specify `group` for project tasks (default: "tasks")

### `update` — Modify a task
- Use `task_id` (title substring) to identify the task
- Can update `status`, `priority`, `due`, `scheduled`, `start`, `title`

### `complete` — Mark a task done
- Shorthand for setting status to "done" and adding completion date
- For project groups: hints when all tasks are done (archive prompt)

### `reopen` — Mark a completed task as todo again
- Removes the completion date

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
tasks(action="create", group="auth-rewrite", title="Extract auth middleware", priority="high")
tasks(action="create", group="auth-rewrite", title="Add JWT validation")
tasks(action="create", group="auth-rewrite", title="Update tests for new auth flow")
tasks(action="create", group="auth-rewrite", title="Update API documentation")
```

The resulting `auth-rewrite.md` file looks like:
```markdown
- [ ] Extract auth middleware  [priority:: high]  [created:: 2026-03-30]
- [ ] Add JWT validation  [created:: 2026-03-30]
- [ ] Update tests for new auth flow  [created:: 2026-03-30]
- [ ] Update API documentation  [created:: 2026-03-30]
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
