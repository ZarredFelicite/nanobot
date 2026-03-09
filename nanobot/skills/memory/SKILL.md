---
name: memory
description: Subconscious memory with automatic extraction and semantic recall.
always: true
---

# Memory

## Structure

Your memory is organized as interlinked markdown notes:

- `memory/entities/` — People, projects, tools, organizations. One file per entity.
- `memory/preferences/` — User preferences, habits, workflow choices. One file per topic.
- `memory/decisions/` — Technical decisions with rationale. One file per decision.
- `memory/history/` — Daily logs (`YYYY-MM-DD.md`), weekly and monthly summaries.

Notes use `[[Name]]` wikilinks to cross-reference each other.

## How It Works

- **Auto-extraction**: Conversations are automatically analyzed and memories are extracted into the appropriate category files.
- **Auto-surfacing**: Relevant memories are injected into your context each turn based on the user's message.
- **Explicit recall**: Use the `memory_search` tool to search for specific memories.

## Manual Updates

You can also read and edit memory files directly with `read_file` and `edit_file`:
- `memory/entities/alice.md` — Update facts about a person
- `memory/preferences/editor.md` — Record a user preference
- `memory/decisions/use-postgresql.md` — Document a decision

## History

Daily activity is logged in `memory/history/YYYY-MM-DD.md`. Weekly and monthly summaries are generated automatically over time.
