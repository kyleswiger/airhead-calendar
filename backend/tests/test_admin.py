from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from airhead import admin
from airhead.repo.dynamo import DynamoEventRepo, DynamoMemberRepo
from conftest import TEST_TABLE
from fakes import make_event


@pytest.fixture
def table(dynamo_resource: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AIRHEAD_TABLE", TEST_TABLE)
    return dynamo_resource


def test_add_and_list_members(table: Any, capsys: pytest.CaptureFixture[str]) -> None:
    rc = admin.main(
        ["add-member", "--household", "hh_f", "--id", "mem_kim", "--name", "Kim"]
        + ["--role", "adult", "--color", "#7aa2f7"]
    )
    assert rc == 0
    member = DynamoMemberRepo().get("hh_f", "mem_kim")
    assert member is not None and member.display_name == "Kim" and member.is_adult

    assert admin.main(["list-members", "--household", "hh_f"]) == 0
    assert "mem_kim\tadult\t#7aa2f7\tKim" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("member_id", "color"), [("Kim", "#7aa2f7"), ("mem_kim", "blue"), ("mem_KIM", "#7aa2f7")]
)
def test_add_member_rejects_bad_input(table: Any, member_id: str, color: str) -> None:
    rc = admin.main(
        ["add-member", "--household", "hh_f", "--id", member_id, "--name", "Kim"]
        + ["--role", "adult", "--color", color]
    )
    assert rc == 2
    assert DynamoMemberRepo().list("hh_f") == []


def test_move_event_between_households(table: Any) -> None:
    admin.main(
        ["add-member", "--household", "hh_f", "--id", "mem_kim", "--name", "Kim"]
        + ["--role", "adult", "--color", "#7aa2f7"]
    )
    events = DynamoEventRepo()
    events.put(
        make_event(
            "evt_real",
            title="Night market",
            start=datetime(2026, 10, 10, 22, tzinfo=UTC),
            end=datetime(2026, 10, 11, 1, tzinfo=UTC),
            owner="mem_alex",
        )
    )

    rc = admin.main(
        ["move-event", "--from", "hh_1", "--to", "hh_f", "--event", "evt_real"]
        + ["--owner", "mem_kim"]
    )
    assert rc == 0
    moved = events.get("hh_f", "evt_real")
    assert moved is not None and moved.owner_member_id == "mem_kim" and moved.deleted_at is None
    original = events.get("hh_1", "evt_real")
    assert original is not None and original.deleted_at is not None


def test_move_event_requires_target_member(table: Any) -> None:
    DynamoEventRepo().put(
        make_event(
            "evt_real",
            start=datetime(2026, 10, 10, 22, tzinfo=UTC),
            end=datetime(2026, 10, 11, 1, tzinfo=UTC),
        )
    )
    rc = admin.main(
        ["move-event", "--from", "hh_1", "--to", "hh_f", "--event", "evt_real"]
        + ["--owner", "mem_nobody"]
    )
    assert rc == 1
