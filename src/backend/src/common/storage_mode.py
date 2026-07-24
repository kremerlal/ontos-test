"""Storage deployment modes for Ontos.

Modes:
- ``lakebase`` / ``postgres`` — transactional Postgres (Lakebase OAuth or external PG)
- ``uc_readonly`` — legacy read-only UC browse (no writes)
- ``uc_native`` — UC Delta + Volumes as system of record (no Postgres)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.common.config import Settings


class StorageMode(str, Enum):
    """How the app persists operational metadata."""

    LAKEBASE = "lakebase"
    POSTGRES = "postgres"
    UC_READONLY = "uc_readonly"
    UC_NATIVE = "uc_native"


# Retired in uc_native — still OLTP-only for lakebase/postgres profiles.
OLTP_REQUIRED_CAPABILITIES: List[str] = [
    "crud_entities",
    "rbac",
    "workflows",
    "rdf_glossary_edit",
    "access_grants",
    "agreements",
    "mdm",
    "settings_admin",
    "demo_seed_sql",
]

UC_CAPABILITIES: List[str] = [
    "catalog_commander",
    "data_catalog",
    "lineage",
    "schema_import",
    "uc_grants",
    "uc_tag_sync",
    "volume_files",
    "uc_mirror_export",
    "audit_volume",
]

UC_READONLY_CAPABILITIES: List[str] = [
    "catalog_commander",
    "data_catalog",
    "lineage",
    "schema_import_read",
    "uc_mirror_read",
    "audit_volume",
]

# Capabilities advertised for STORAGE_MODE=uc_native.
# Only include features with UC managers wired in ``uc_native/startup.py``.
# Partial panels (MDM match runs, compliance scoring, agreements) stay off the
# list until Delta writers exist beyond config CRUD stubs.
UC_NATIVE_CAPABILITIES: List[str] = [
    *UC_CAPABILITIES,
    "crud_entities",
    "rbac",
    "workflows",
    "rdf_glossary_edit",
    "access_grants",
    "mdm",
    "term_mapping",
    "ontology_generator",
    "settings_admin",
    "demo_seed_delta",
    "uc_native_sor",
]


def resolve_storage_mode(settings: "Settings") -> StorageMode:
    """Infer storage mode from explicit env or connection settings."""
    explicit = (getattr(settings, "STORAGE_MODE", None) or "").strip().lower()
    if explicit == StorageMode.UC_NATIVE.value:
        return StorageMode.UC_NATIVE
    if explicit == StorageMode.UC_READONLY.value:
        return StorageMode.UC_READONLY
    if explicit == StorageMode.POSTGRES.value:
        return StorageMode.POSTGRES
    if explicit == StorageMode.LAKEBASE.value:
        return StorageMode.LAKEBASE

    if getattr(settings, "DB_USE_PASSWORD_AUTH", False):
        return StorageMode.POSTGRES

    env = (settings.ENV or "").upper()
    if env.startswith("LOCAL"):
        return StorageMode.POSTGRES

    if not getattr(settings, "PGHOST", None) and not getattr(settings, "DATABASE_URL", None):
        # Default no-Postgres deployments to uc_native (write-capable UC SoR).
        return StorageMode.UC_NATIVE

    return StorageMode.LAKEBASE


def requires_oltp_database(mode: StorageMode) -> bool:
    return mode in (StorageMode.LAKEBASE, StorageMode.POSTGRES)


def uses_uc_native_storage(mode: StorageMode) -> bool:
    return mode == StorageMode.UC_NATIVE


def uses_lakebase_oauth(settings: "Settings", mode: Optional[StorageMode] = None) -> bool:
    """True when DB connections should use Lakebase OAuth token refresh."""
    mode = mode or resolve_storage_mode(settings)
    if mode != StorageMode.LAKEBASE:
        return False
    if settings.ENV.upper().startswith("LOCAL"):
        return False
    if settings.DB_USE_PASSWORD_AUTH:
        return False
    return True


def get_storage_capabilities(
    mode: StorageMode,
    *,
    oltp_configured: bool = True,
    uc_configured: bool = True,
) -> Dict[str, Any]:
    """Return capability flags for API/UI consumption."""
    available: List[str] = []
    unavailable: List[str] = []

    if mode == StorageMode.UC_NATIVE:
        available.extend(UC_NATIVE_CAPABILITIES)
        writes_enabled = True
        oltp_configured = False
    elif mode == StorageMode.UC_READONLY or not oltp_configured:
        available.extend(UC_READONLY_CAPABILITIES)
        unavailable.extend(OLTP_REQUIRED_CAPABILITIES)
        writes_enabled = False
    else:
        available.extend(UC_CAPABILITIES)
        available.extend(OLTP_REQUIRED_CAPABILITIES)
        writes_enabled = True

    if not uc_configured:
        for cap in list(UC_CAPABILITIES):
            if cap in available:
                available.remove(cap)
                unavailable.append(cap)

    return {
        "mode": mode.value,
        "oltp_configured": oltp_configured,
        "uc_configured": uc_configured,
        "writes_enabled": writes_enabled,
        "available_capabilities": sorted(set(available)),
        "unavailable_capabilities": sorted(set(unavailable)),
        "lakebase_required": mode == StorageMode.LAKEBASE,
        "postgres_alternative": mode in (StorageMode.POSTGRES, StorageMode.LAKEBASE),
        "uc_native_sor": mode == StorageMode.UC_NATIVE,
    }
