"""Bootstrap UC Delta schema and seed data."""

from __future__ import annotations

from databricks.sdk import WorkspaceClient

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore
from src.common.uc_native.rbac import UcNativeRbacStore
from src.common.uc_native.tables import APP_TABLES

logger = get_logger(__name__)


def bootstrap_uc_native(ws_client: WorkspaceClient, settings: Settings) -> DeltaStore:
    """Ensure all UC app tables exist and seed default roles."""
    store = DeltaStore(ws_client, settings)
    store.ensure_tables(list(APP_TABLES.keys()))
    rbac = UcNativeRbacStore(store, settings)
    rbac.seed_default_roles()
    if getattr(settings, "APP_DEMO_MODE", False):
        try:
            from src.common.uc_native.demo_seed import seed_demo_delta

            seed_demo_delta(store)
        except Exception as exc:
            logger.warning("UC-native demo seed skipped: %s", exc)
    logger.info("UC native bootstrap complete (schema=%s)", store.schema_name())
    return store
