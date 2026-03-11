from pathlib import Path

from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService


def test_cron_tool_forces_owner_delivery_target(tmp_path: Path) -> None:
    cron = CronService(tmp_path / "jobs.json")
    tool = CronTool(cron_service=cron, owner_channel="telegram", owner_chat_id="8281248569")
    tool.set_context("cli", "main")

    result = tool._add_job(
        message="ping me",
        every_seconds=60,
        cron_expr=None,
        tz=None,
        at=None,
        channel="email",
        to="someone@example.com",
    )

    assert "delivery=telegram:8281248569" in result
    jobs = cron.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.channel == "telegram"
    assert jobs[0].payload.to == "8281248569"
