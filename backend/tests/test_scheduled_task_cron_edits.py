from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.gateway.scheduled_task_validation import _recompute_schedule
from deerflow.config.database_config import DatabaseConfig
from deerflow.config.scheduler_config import SchedulerConfig
from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config
from deerflow.persistence.scheduled_tasks import ScheduledTaskRepository

_DUE = datetime(2027, 1, 1, 9, tzinfo=UTC)


def _existing(**overrides):
    return {
        "schedule_type": "cron",
        "schedule_spec": {"cron": "0 9 * * *"},
        "timezone": "UTC",
        "status": "enabled",
        "next_run_at": _DUE.isoformat(),
        **overrides,
    }


@pytest.mark.parametrize("status", ["enabled", "paused"])
@pytest.mark.parametrize("seconds_after_due", [-1, 0, 1])
def test_unchanged_cron_edit_keeps_pending_occurrence(status, seconds_after_due):
    existing = _existing(status=status)
    updates = {"title": "Renamed report", "schedule_spec": {"cron": "  0  9 * * * "}, "timezone": "UTC"}

    _recompute_schedule(existing, updates, scheduler_config=SchedulerConfig(), now=_DUE + timedelta(seconds=seconds_after_due))

    assert updates["title"] == "Renamed report"
    assert updates["schedule_spec"] == {"cron": "0 9 * * *"}
    assert datetime.fromisoformat(str(updates.get("next_run_at", existing["next_run_at"]))) == _DUE
    assert "status" not in updates


@pytest.mark.parametrize(
    ("existing_changes", "fields", "expected"),
    [
        ({}, {"schedule_spec": {"cron": "0 10 * * *"}}, _DUE + timedelta(hours=1)),
        ({}, {"timezone": "Asia/Shanghai"}, _DUE + timedelta(hours=16)),
        ({}, {"schedule_type": "interval", "schedule_spec": {"every_seconds": 90}}, _DUE + timedelta(seconds=91)),
        ({"schedule_type": "interval", "schedule_spec": {"every_seconds": 3600}}, {"schedule_type": "cron", "schedule_spec": {"cron": "0 9 * * *"}}, _DUE + timedelta(days=1)),
        ({"status": "completed"}, {"schedule_spec": {"cron": "0 9 * * *"}}, _DUE + timedelta(days=1)),
        ({"status": "failed"}, {"schedule_spec": {"cron": "0 9 * * *"}}, _DUE + timedelta(days=1)),
        ({"status": "cancelled"}, {"schedule_spec": {"cron": "0 9 * * *"}}, _DUE + timedelta(days=1)),
        ({"next_run_at": None}, {"schedule_spec": {"cron": "0 9 * * *"}}, _DUE + timedelta(days=1)),
    ],
    ids=["changed-cron", "changed-zone", "to-interval", "from-interval", "completed", "failed", "cancelled", "missing-occurrence"],
)
def test_changed_or_reactivated_cron_still_recomputes(existing_changes, fields, expected):
    existing = _existing(**existing_changes)
    updates = dict(fields)

    _recompute_schedule(existing, updates, scheduler_config=SchedulerConfig(), now=_DUE + timedelta(seconds=1))

    assert updates["next_run_at"] == expected
    if existing["status"] in {"completed", "failed", "cancelled"}:
        assert updates["status"] == "enabled"
    else:
        assert "status" not in updates


@pytest.mark.parametrize("cron", [None, "not cron", "61 9 * * *"])
def test_cron_edits_still_validate_the_expression(cron):
    with pytest.raises(HTTPException) as error:
        _recompute_schedule(_existing(), {"schedule_spec": {"cron": cron}}, scheduler_config=SchedulerConfig(), now=_DUE)

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_unchanged_cron_edit_does_not_restore_a_stale_occurrence(tmp_path):
    await init_engine_from_config(DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path)))
    try:
        repo = ScheduledTaskRepository(get_session_factory())
        existing = await repo.create(
            task_id="cron-edit",
            user_id="owner",
            thread_id=None,
            context_mode="fresh_thread_per_run",
            assistant_id="lead_agent",
            title="Original title",
            prompt="Prepare report",
            schedule_type="cron",
            schedule_spec={"cron": "0 9 * * *"},
            timezone="UTC",
            next_run_at=_DUE,
        )
        updates = {"title": "Renamed report", "schedule_spec": dict(existing["schedule_spec"])}
        _recompute_schedule(existing, updates, scheduler_config=SchedulerConfig(), now=_DUE - timedelta(seconds=1))

        # The scheduler advances after the edit reads the task, before it writes.
        advanced = _DUE + timedelta(days=1)
        assert await repo.update_after_launch(
            "cron-edit",
            status="enabled",
            next_run_at=advanced,
            last_run_at=_DUE,
            last_run_id="run-report",
            last_thread_id="thread-report",
            last_error=None,
            increment_run_count=True,
        )
        await repo.update("cron-edit", user_id="owner", updates=updates, require_mutable=True)

        stored = await repo.get("cron-edit", user_id="owner")
        assert stored["title"] == "Renamed report"
        assert stored["last_run_id"] == "run-report"
        assert stored["run_count"] == 1
        assert datetime.fromisoformat(stored["next_run_at"]) == advanced
        assert await repo.claim_due_tasks(now=_DUE + timedelta(seconds=1), lease_owner="worker", lease_seconds=120, limit=10) == []
    finally:
        await close_engine()
