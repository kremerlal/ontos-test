"""Optional demo seed for UC-native Delta tables.

Invoked from bootstrap when ``APP_DEMO_MODE`` is true. Keeps seed idempotent by
upserting known demo IDs rather than wiping customer data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore

logger = get_logger(__name__)

_DEMO_TEAM_ID = "demo-team-platform"
_DEMO_DOMAIN_ID = "demo-domain-core"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_demo_delta(store: DeltaStore) -> Dict[str, Any]:
    """Insert a small demo graph into Delta entity tables."""
    created = {"teams": 0, "domains": 0, "tags": 0}

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
        "id": "demo-tag-pii",
        "name": "demo.pii",
        "updated_at": _now(),
        "snapshot_json": json.dumps(
            {"id": "demo-tag-pii", "name": "demo.pii", "description": "Demo PII tag"}
        ),
    }
    store.merge_row("tags", tag)
    created["tags"] = 1

    logger.info("UC-native demo seed applied: %s", created)
    return created
