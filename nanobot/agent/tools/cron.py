"""Cron tool for scheduling reminders and tasks."""

from contextvars import ContextVar
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(
        self,
        cron_service: CronService,
        owner_channel: str = "",
        owner_chat_id: str = "",
    ):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
        self._owner_channel = owner_channel
        self._owner_chat_id = owner_chat_id
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    def set_owner_target(self, channel: str, chat_id: str) -> None:
        """Restrict delivery to a fixed owner target."""
        self._owner_channel = channel
        self._owner_chat_id = chat_id

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            "For add, optional channel/to can target a specific destination."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00')",
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
                "channel": {
                    "type": "string",
                    "description": "Delivery channel override for add (e.g. 'telegram')",
                },
                "to": {
                    "type": "string",
                    "description": "Destination chat/user ID override for add",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        message = kwargs.get("message", "")
        every_seconds = kwargs.get("every_seconds")
        cron_expr = kwargs.get("cron_expr")
        tz = kwargs.get("tz")
        at = kwargs.get("at")
        job_id = kwargs.get("job_id")
        channel = kwargs.get("channel")
        to = kwargs.get("to")

        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(message, every_seconds, cron_expr, tz, at, channel, to)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        channel: str | None,
        to: str | None,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not (self._owner_channel and self._owner_chat_id) and (
            not self._channel or not self._chat_id
        ):
            return "Error: no session context (channel/chat_id)"
        if (channel and not to) or (to and not channel):
            return "Error: channel and to must be provided together"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz)
            except (KeyError, Exception):
                return f"Error: unknown timezone '{tz}'"

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            from datetime import datetime

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        if self._owner_channel and self._owner_chat_id:
            target_channel = self._owner_channel
            target_to = self._owner_chat_id
        else:
            target_channel = channel or self._channel
            target_to = to or self._chat_id

        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=target_channel,
            to=target_to,
            delete_after_run=delete_after,
        )
        return f"Created job '{job.name}' (id: {job.id}, delivery={target_channel}:{target_to})"

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
