import json
from datetime import datetime

from nanobot.session.manager import SessionManager


def test_load_preserves_updated_at_from_metadata(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    path = manager._get_session_path("opencode:s1")

    created_at = "2026-03-08T12:00:00"
    updated_at = "2026-03-09T20:15:30"
    path.write_text(
        json.dumps(
            {
                "_type": "metadata",
                "key": "opencode:s1",
                "created_at": created_at,
                "updated_at": updated_at,
                "metadata": {},
                "last_consolidated": 0,
            }
        )
        + "\n"
        + json.dumps({"role": "user", "content": "hi", "timestamp": created_at})
        + "\n",
        encoding="utf-8",
    )

    session = manager.get_or_create("opencode:s1")

    assert session.created_at == datetime.fromisoformat(created_at)
    assert session.updated_at == datetime.fromisoformat(updated_at)


def test_load_falls_back_updated_at_to_last_message_timestamp(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    path = manager._get_session_path("opencode:s2")

    created_at = "2026-03-08T12:00:00"
    last_msg_ts = "2026-03-10T23:21:37"
    path.write_text(
        json.dumps(
            {
                "_type": "metadata",
                "key": "opencode:s2",
                "created_at": created_at,
                "metadata": {},
                "last_consolidated": 0,
            }
        )
        + "\n"
        + json.dumps({"role": "user", "content": "hello", "timestamp": last_msg_ts})
        + "\n",
        encoding="utf-8",
    )

    session = manager.get_or_create("opencode:s2")

    assert session.updated_at == datetime.fromisoformat(last_msg_ts)
