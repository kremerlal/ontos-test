"""Unit tests for UC-native deploy hygiene helpers."""

import uuid

from src.common.uc_native.demo_seed import seed_demo_delta
from src.workflows.common.uc_native_runtime import (
    lakebase_skip_message,
    resolve_job_storage_mode,
    should_use_lakebase_oltp,
)

_DEMO_NS = uuid.UUID("d0000000-0000-4000-8000-000000000001")
_DEMO_TEAM_ID = str(uuid.uuid5(_DEMO_NS, "team:platform"))
_DEMO_DOMAIN_ID = str(uuid.uuid5(_DEMO_NS, "domain:core"))


def test_resolve_job_storage_mode_prefers_explicit(monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    assert resolve_job_storage_mode("uc_native") == "uc_native"


def test_should_skip_lakebase_for_uc_native():
    assert should_use_lakebase_oltp(storage_mode="uc_native", lakebase_instance_name="lb") is False
    assert should_use_lakebase_oltp(storage_mode="lakebase", lakebase_instance_name="lb") is True
    assert should_use_lakebase_oltp(storage_mode="lakebase", lakebase_instance_name="") is False


def test_lakebase_skip_message_mentions_delta():
    assert "Delta" in lakebase_skip_message("uc_tag_sync")


def test_demo_seed_writes_known_ids():
    class FakeStore:
        def __init__(self):
            self.rows = {}

        def merge_row(self, table, row):
            self.rows.setdefault(table, {})[row["id"]] = row

    store = FakeStore()
    created = seed_demo_delta(store)
    assert created["teams"] == 1
    assert _DEMO_TEAM_ID in store.rows["teams"]
    assert _DEMO_DOMAIN_ID in store.rows["data_domains"]
