"""Subconscious: proactive memory extraction and retrieval via hierarchical markdown + qmd."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.qmd import QMDClient
from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.config.schema import SubconsciousConfig
    from nanobot.providers.base import LLMProvider


def _safe_filename(name: str) -> str:
    """Convert a name to a safe filename (lowercase, hyphens, no special chars)."""
    s = name.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = s.strip("-")
    return s[:80] or "unnamed"


_EXTRACT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memories",
            "description": "Save extracted memories from the conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "description": "Named entities mentioned (people, machines, programs, etc).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Entity name (e.g. 'Alice', 'Sankara', 'FreshRSS')",
                                },
                                "path": {
                                    "type": "string",
                                    "description": "Relative path under memory/ to store the note (e.g. 'entities/people', 'entities/machines'). Use existing directories from the listing, or create a new subdirectory if none fit.",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Markdown content about this entity. Use [[Name]] wikilinks to reference other entities.",
                                },
                                "action": {
                                    "type": "string",
                                    "enum": ["create", "update", "delete"],
                                    "description": "Create a new note, update an existing one, or delete an obsolete one.",
                                },
                            },
                            "required": ["name", "path", "action"],
                        },
                    },
                    "notes": {
                        "type": "array",
                        "description": "Preferences, decisions, or other non-entity memories to store.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Note name/title (e.g. 'communication style', 'Use PostgreSQL')",
                                },
                                "path": {
                                    "type": "string",
                                    "description": "Relative path under memory/ to store the note (e.g. 'preferences', 'decisions'). Use existing directories from the listing, or create a new one if none fit.",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Markdown content. Use [[Name]] wikilinks to cross-reference.",
                                },
                                "action": {
                                    "type": "string",
                                    "enum": ["create", "update", "delete"],
                                    "description": "Create a new note, update an existing one, or delete an obsolete one.",
                                },
                            },
                            "required": ["name", "path", "action"],
                        },
                    },
                },
                "required": [],
            },
        },
    }
]


class SubconsciousService:
    """Proactive memory: extracts facts from conversations into markdown notes,
    retrieves relevant memories via qmd semantic search."""

    _HISTORY_IDLE_THRESHOLD_S = 1800  # 30 minutes

    def __init__(self, workspace: Path, config: SubconsciousConfig):
        self._config = config
        self._workspace = workspace
        self._memory_dir = workspace / "memory"
        self._provider: LLMProvider | None = None
        self._buffer: list[dict[str, str]] = []
        self._last_flush: float = time.monotonic()
        self._bg_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

        # Conversation buffer for history summarization (separate from extraction buffer)
        self._conversation_buffer: list[dict[str, str]] = []
        self._last_message_time: float = 0.0
        self._conversation_session_key: str = ""

        self._qmd = QMDClient(
            collection_name=config.qmd_collection_name,
            collection_path=self._memory_dir,
        )

    def set_provider(self, provider: LLMProvider) -> None:
        self._provider = provider

    def _ensure_dirs(self) -> None:
        ensure_dir(self._memory_dir / "history")

    async def initialize(self) -> None:
        self._ensure_dirs()
        if self._qmd.available:
            await self._qmd.ensure_collection()
            await self._qmd.reindex()
            logger.info("Subconscious initialized (qmd collection: {})", self._config.qmd_collection_name)
        else:
            logger.warning("qmd not available — subconscious recall will be limited")

    def start_background_task(self) -> None:
        self._bg_task = asyncio.create_task(self._periodic_flush())

    async def _periodic_flush(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                now = time.monotonic()
                # Flush extraction buffer on time threshold
                if self._buffer and (now - self._last_flush) >= self._config.batch_time_threshold_s:
                    await self._flush()
                # Summarize conversation to history after idle period
                if (
                    self._conversation_buffer
                    and self._last_message_time > 0
                    and (now - self._last_message_time) >= self._HISTORY_IDLE_THRESHOLD_S
                ):
                    await self._summarize_conversation()
        except asyncio.CancelledError:
            pass

    def feed_messages(self, messages: list[dict], session_key: str = "") -> None:
        """Buffer user/assistant messages for extraction and conversation history."""
        for msg in messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            entry = {"role": role, "content": content}
            self._buffer.append(entry)
            self._conversation_buffer.append(entry)

        if self._buffer:
            self._last_message_time = time.monotonic()
            self._conversation_session_key = session_key

        if len(self._buffer) >= self._config.batch_message_threshold:
            asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        if not self._buffer or not self._provider:
            return

        batch = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.monotonic()

        try:
            await self._extract(batch)
        except Exception:
            logger.exception("Subconscious extraction failed")

    async def _extract(self, messages: list[dict[str, str]]) -> None:
        """Call the extraction LLM to identify entities, preferences, decisions."""
        if not self._provider:
            return

        # Build context: list existing files so the LLM knows what to update
        existing = self._list_existing_notes()
        conversation = "\n\n".join(f"[{m['role']}]: {m['content']}" for m in messages)

        prompt = f"""Extract memorable facts from this conversation. Focus on:
- **Entities**: People, machines, programs, or other named things. Use the `path` field to place them in the right directory (see existing structure below).
- **Notes**: Preferences, decisions, or any other facts worth remembering. Use the `path` field to place them in the right directory.

Use [[Name]] wikilinks to cross-reference entities in your content.

If a note already exists (listed below), use action="update" and write the COMPLETE updated content (not just the delta). If the new information contradicts old information, replace the outdated facts. Create new subdirectories as needed if nothing existing fits.

If nothing noteworthy was discussed (e.g. casual greetings, simple questions), call save_memories with empty arrays. Only extract genuinely persistent facts.

## Existing Notes
{existing or "(none yet)"}

## Conversation
{conversation}"""

        try:
            response = await self._provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a memory extraction agent. Analyze conversations and extract structured memories by calling the save_memories tool. Be selective — only extract genuinely useful information.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_EXTRACT_TOOL,
                model=self._config.extraction_model,
            )

            if not response.has_tool_calls:
                logger.debug("Extraction LLM did not call save_memories, skipping")
                return

            args = response.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                return

            await self._write_memories(args)
        except Exception:
            logger.exception("Subconscious extraction LLM call failed")

    def _list_existing_notes(self) -> str:
        """List existing note files by walking the memory directory tree."""
        lines = []
        if not self._memory_dir.exists():
            return ""
        for dirpath in sorted(self._memory_dir.rglob("*")):
            if not dirpath.is_dir():
                continue
            rel = dirpath.relative_to(self._memory_dir)
            # Skip history — it's managed separately
            if str(rel).startswith("history"):
                continue
            files = sorted(dirpath.glob("*.md"))
            if files:
                names = [f.stem for f in files]
                lines.append(f"**{rel}/**: {', '.join(names)}")
        return "\n".join(lines)

    async def _write_memories(self, args: dict[str, Any]) -> None:
        """Write extracted memories to markdown files."""
        async with self._write_lock:
            notes_written = 0

            for item in [*args.get("entities", []), *args.get("notes", [])]:
                name = item.get("name", "")
                path = item.get("path", "")
                action = item.get("action", "create")
                if not name or not path:
                    continue
                if action == "delete":
                    self._delete_note(path, name)
                    notes_written += 1
                else:
                    content = item.get("content", "")
                    if content:
                        self._write_note(path, name, content)
                        notes_written += 1

            if notes_written and self._qmd.available:
                await self._qmd.reindex()
                logger.debug("Subconscious: wrote {} note(s) and reindexed", notes_written)

    def _delete_note(self, path: str, name: str) -> None:
        """Delete a note file."""
        filename = _safe_filename(name) + ".md"
        filepath = (self._memory_dir / path / filename).resolve()
        if not str(filepath).startswith(str(self._memory_dir.resolve())):
            logger.warning("Blocked path traversal attempt: {}", path)
            return
        if filepath.exists():
            filepath.unlink()
            logger.debug("Deleted {}/{}", path, filename)
            # Remove empty parent directories up to memory root
            parent = filepath.parent
            while parent != self._memory_dir.resolve() and not any(parent.iterdir()):
                parent.rmdir()
                logger.debug("Removed empty directory: {}", parent)
                parent = parent.parent

    def _write_note(self, path: str, name: str, content: str) -> None:
        """Write or overwrite a note file."""
        filename = _safe_filename(name) + ".md"
        filepath = (self._memory_dir / path / filename).resolve()
        # Prevent path traversal
        if not str(filepath).startswith(str(self._memory_dir.resolve())):
            logger.warning("Blocked path traversal attempt: {}", path)
            return
        filepath.parent.mkdir(parents=True, exist_ok=True)

        header = f"# {name}\n\n"
        filepath.write_text(header + content + "\n", encoding="utf-8")
        logger.debug("Wrote {}/{}", path, filename)

    def _append_history(self, entry: str, session_key: str = "") -> None:
        """Append a timestamped entry to today's history file."""
        today = datetime.now().strftime("%Y-%m-%d")
        filepath = self._memory_dir / "history" / f"{today}.md"
        filepath.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%H:%M")
        session_tag = f" [{session_key}]" if session_key else ""
        line = f"[{timestamp}]{session_tag} {entry.strip()}\n\n"

        if not filepath.exists():
            filepath.write_text(f"# {today}\n\n{line}", encoding="utf-8")
        else:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line)

    _CLASSIFIER_PROMPT = (
        'You are a classifier. Given the last assistant message and the current user message, '
        'respond with ONLY "yes" or "no".\n'
        'Answer "yes" if retrieving the user\'s stored memories (preferences, past decisions, '
        'people, projects) would help answer the current message.\n'
        'Answer "no" for greetings, simple questions, math, code syntax, or anything that '
        'doesn\'t benefit from personal context.'
    )

    async def should_inject(self, user_message: str, prev_assistant: str | None = None) -> bool:
        """Fast LLM classifier to decide if memory injection would be helpful."""
        if not self._provider:
            return True  # Default to injecting if no provider

        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._CLASSIFIER_PROMPT},
        ]
        if prev_assistant:
            messages.append({"role": "assistant", "content": prev_assistant})
        messages.append({"role": "user", "content": user_message})

        try:
            response = await self._provider.chat(
                messages=messages,
                model=self._config.classifier_model,
                max_tokens=5,
                temperature=0,
            )
            answer = (response.content or "").strip().lower()
            result = answer.startswith("yes")
            logger.debug("Memory classifier: {} (answer={})", result, answer)
            return result
        except Exception:
            logger.debug("Memory classifier failed, defaulting to inject")
            return True

    @staticmethod
    def _format_results(results: list[dict[str, Any]], budget: int, min_score: float = 0.3) -> str:
        """Format qmd results into readable text within a token budget."""
        lines: list[str] = []
        token_estimate = 0
        for r in results:
            score = r.get("score", 0)
            if score < min_score:
                continue
            snippet = r.get("snippet", "")
            title = r.get("title", "")
            filepath = r.get("file", "")

            # Clean up snippet — remove diff-style headers
            snippet = re.sub(r"^@@.*?@@\s*", "", snippet, flags=re.MULTILINE).strip()
            if not snippet:
                continue

            # Extract path after collection name (e.g. "entities/people")
            source = ""
            if filepath:
                parts = filepath.replace("qmd://", "").split("/")
                if len(parts) >= 3:
                    source = f" ({'/'.join(parts[1:-1])})"

            entry = f"**{title}**{source} [{score:.0%}]\n{snippet}"
            entry_tokens = len(entry) // 4
            if token_estimate + entry_tokens > budget:
                break
            lines.append(entry)
            token_estimate += entry_tokens

        return "\n\n".join(lines)

    async def recall(self, query: str, budget: int | None = None, n: int | None = None) -> str:
        """Fast vector recall for auto-injection into context."""
        if not self._qmd.available:
            return ""

        budget = budget or self._config.auto_inject_budget
        n = n or self._config.auto_inject_results

        if self._buffer and self._provider:
            await self._flush()

        results = await self._qmd.vsearch(query, n=n)
        if not results:
            return ""

        return self._format_results(results, budget)

    async def search(self, query: str, budget: int = 4000, n: int = 10) -> str:
        """Full semantic search with reranking for explicit memory_search tool use."""
        if not self._qmd.available:
            return ""

        if self._buffer and self._provider:
            await self._flush()

        results = await self._qmd.query(query, n=n)
        if not results:
            return ""

        return self._format_results(results, budget, min_score=0.2)

    async def compact_history(self) -> None:
        """Generate weekly and monthly summaries for old history files."""
        if not self._config.compaction_enabled or not self._provider:
            return

        history_dir = self._memory_dir / "history"
        if not history_dir.exists():
            return

        today = datetime.now().date()
        daily_files = sorted(history_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))

        # Group daily files by ISO week for weekly summaries
        weeks: dict[str, list[Path]] = {}
        for f in daily_files:
            try:
                date = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            age_days = (today - date).days
            if age_days < 7:
                continue
            iso_year, iso_week, _ = date.isocalendar()
            week_key = f"week-{iso_year}-W{iso_week:02d}"
            weeks.setdefault(week_key, []).append(f)

        for week_key, files in weeks.items():
            summary_file = history_dir / f"{week_key}.md"
            if summary_file.exists():
                continue
            await self._generate_summary(files, summary_file, week_key)

        # Monthly summaries from weekly files older than 30 days
        weekly_files = sorted(history_dir.glob("week-*.md"))
        months: dict[str, list[Path]] = {}
        for f in weekly_files:
            # Parse week date to find the month
            match = re.match(r"week-(\d{4})-W(\d{2})", f.stem)
            if not match:
                continue
            year, week_num = int(match.group(1)), int(match.group(2))
            # Approximate: week 1 = January, etc.
            approx_month = min(12, max(1, (week_num - 1) * 7 // 30 + 1))
            month_key = f"month-{year}-{approx_month:02d}"
            # Only include if old enough
            try:
                content = f.read_text(encoding="utf-8")
                # Check if any referenced dates are old enough
                date_matches = re.findall(r"\d{4}-\d{2}-\d{2}", content)
                if date_matches:
                    latest = max(datetime.strptime(d, "%Y-%m-%d").date() for d in date_matches)
                    if (today - latest).days < 30:
                        continue
            except (ValueError, OSError):
                continue
            months.setdefault(month_key, []).append(f)

        for month_key, files in months.items():
            summary_file = history_dir / f"{month_key}.md"
            if summary_file.exists():
                continue
            await self._generate_summary(files, summary_file, month_key)

    async def _generate_summary(
        self, source_files: list[Path], output_file: Path, label: str
    ) -> None:
        """Generate a summary file from multiple source files."""
        if not self._provider:
            return

        combined = []
        for f in source_files:
            try:
                combined.append(f.read_text(encoding="utf-8"))
            except OSError:
                continue

        if not combined:
            return

        try:
            response = await self._provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a summarization agent. Condense daily/weekly history logs into a coherent summary. Preserve key facts, decisions, and events. Use [[Name]] wikilinks for entities.",
                    },
                    {
                        "role": "user",
                        "content": f"Summarize these history entries for {label}:\n\n{'---'.join(combined)}",
                    },
                ],
                model=self._config.extraction_model,
            )
            if response.content:
                output_file.write_text(f"# {label}\n\n{response.content}\n", encoding="utf-8")
                logger.info("Generated history summary: {}", output_file.name)
        except Exception:
            logger.exception("Failed to generate summary for {}", label)

    async def _summarize_conversation(self) -> None:
        """Summarize the buffered conversation into a single history entry."""
        if not self._conversation_buffer or not self._provider:
            return

        batch = self._conversation_buffer[:]
        session_key = self._conversation_session_key
        self._conversation_buffer.clear()
        self._last_message_time = 0.0
        self._conversation_session_key = ""

        conversation = "\n".join(f"[{m['role']}]: {m['content']}" for m in batch)

        try:
            response = await self._provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "Summarize this conversation in 1-3 concise sentences. Focus on what was discussed, decided, or accomplished. Use [[Name]] wikilinks for people and entities.",
                    },
                    {"role": "user", "content": conversation},
                ],
                model=self._config.extraction_model,
            )
            if response.content:
                summary = response.content.strip()
                self._append_history(summary, session_key=session_key)
                if self._qmd.available:
                    await self._qmd.reindex()
                logger.info("Conversation summary written to history ({} messages, session={})", len(batch), session_key)
        except Exception:
            logger.exception("Failed to summarize conversation for history")

    async def close(self) -> None:
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass

        # Flush pending extraction
        if self._buffer and self._provider:
            await self._flush()

        # Summarize any remaining conversation to history
        if self._conversation_buffer and self._provider:
            await self._summarize_conversation()

        logger.debug("Subconscious service closed")
