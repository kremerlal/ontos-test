"""UC-native job runtime helpers.

Databricks Jobs historically required Lakebase OAuth (``--lakebase_instance_name``)
to write app metadata via Postgres. In ``STORAGE_MODE=uc_native`` the app SoR is
Delta; notebook/job writers that still call ``build_postgres_engine_from_env``
must soft-skip or be migrated to Delta stores.

Use ``should_use_lakebase_oltp`` in job entrypoints so UC-native deploys do not
fail on missing Lakebase parameters.
"""
from __future__ import annotations

import os
from typing import Optional


def resolve_job_storage_mode(explicit: Optional[str] = None) -> str:
    """Return lakebase | postgres | uc_native | uc_readonly."""
    mode = (explicit or os.environ.get("STORAGE_MODE") or "").strip().lower()
    if mode:
        return mode
    if os.environ.get("PGHOST") or os.environ.get("DATABASE_URL"):
        return "lakebase"
    return "uc_native"


def should_use_lakebase_oltp(
    *,
    storage_mode: Optional[str] = None,
    lakebase_instance_name: Optional[str] = None,
) -> bool:
    """False when the job should not open a Postgres/Lakebase engine."""
    mode = resolve_job_storage_mode(storage_mode)
    if mode in ("uc_native", "uc_readonly"):
        return False
    return bool((lakebase_instance_name or "").strip())


def lakebase_skip_message(job_name: str) -> str:
    return (
        f"[{job_name}] STORAGE_MODE is UC-native/read-only (or lakebase_instance_name "
        "was omitted). Skipping Postgres OLTP writes. Migrate this job to UC Delta "
        "stores under src.common.uc_native before re-enabling scheduled runs."
    )
