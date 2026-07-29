"""Optional demo seed for UC-native Delta tables.

Invoked from bootstrap when ``APP_DEMO_MODE`` is true. Keeps seed idempotent by
upserting known demo IDs rather than wiping customer data.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore

logger = get_logger(__name__)

# Seeded IDs must be real UUIDs: read models such as DataDomainRead type `id`
# as UUID, so a slug id makes the corresponding list endpoint fail. uuid5 keeps
# the IDs stable across restarts so the seed stays idempotent.
_DEMO_NS = uuid.UUID("d0000000-0000-4000-8000-000000000001")
_DEMO_TEAM_ID = str(uuid.uuid5(_DEMO_NS, "team:platform"))
_DEMO_DOMAIN_ID = str(uuid.uuid5(_DEMO_NS, "domain:core"))
_DEMO_TAG_ID = str(uuid.uuid5(_DEMO_NS, "tag:pii"))

# Slug IDs written by earlier versions of this seed. They are removed on
# startup so existing dev environments recover without a manual table wipe.
_LEGACY_DEMO_IDS = {
    "data_domains": "demo-domain-core",
    "teams": "demo-team-platform",
    "tags": "demo-tag-pii",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _purge_legacy_rows(store: DeltaStore) -> None:
    for table, legacy_id in _LEGACY_DEMO_IDS.items():
        try:
            if store.get_by_id(table, legacy_id):
                store.delete_by_id(table, legacy_id)
                logger.info("Removed legacy non-UUID demo row '%s' from %s", legacy_id, table)
        except Exception:
            logger.warning(
                "Could not remove legacy demo row '%s' from %s", legacy_id, table, exc_info=True
            )


def seed_demo_delta(store: DeltaStore) -> Dict[str, Any]:
    """Insert a small demo graph into Delta entity tables."""
    created = {"teams": 0, "domains": 0, "tags": 0}

    _purge_legacy_rows(store)

    domain = {
        "id": _DEMO_DOMAIN_ID,
        "name": "Demo Core Domain",
        "status": "active",
        "updated_at": _now(),
        "snapshot_json": json.dumps(
            {
                "id": _DEMO_DOMAIN_ID,
                "name": "Demo Core Domain",
                "description": "Seeded for APP_DEMO_MODE in uc_native",
                "status": "active",
            }
        ),
    }
    store.merge_row("data_domains", domain)
    created["domains"] = 1

    team = {
        "id": _DEMO_TEAM_ID,
        "name": "Platform Demo Team",
        "status": "active",
        "updated_at": _now(),
        "snapshot_json": json.dumps(
            {
                "id": _DEMO_TEAM_ID,
                "name": "Platform Demo Team",
                "domain_id": _DEMO_DOMAIN_ID,
                "members": [],
                "status": "active",
            }
        ),
    }
    store.merge_row("teams", team)
    created["teams"] = 1

    tag = {
        "id": _DEMO_TAG_ID,
        "name": "demo.pii",
        "updated_at": _now(),
        "snapshot_json": json.dumps(
            {"id": _DEMO_TAG_ID, "name": "demo.pii", "description": "Demo PII tag"}
        ),
    }
    store.merge_row("tags", tag)
    created["tags"] = 1

    logger.info("UC-native demo seed applied: %s", created)
    return created
