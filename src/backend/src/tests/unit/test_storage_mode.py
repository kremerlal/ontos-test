"""Unit tests for storage deployment modes."""

from src.common.config import Settings
from src.common.storage_mode import (
    StorageMode,
    get_storage_capabilities,
    requires_oltp_database,
    resolve_storage_mode,
    uses_lakebase_oauth,
    uses_uc_native_storage,
)


def _settings(**kwargs) -> Settings:
    base = {
        "DATABRICKS_HOST": "https://example.cloud.databricks.com",
        "DATABRICKS_WAREHOUSE_ID": "wh1",
        "DATABRICKS_CATALOG": "app_data",
        "APP_AUDIT_LOG_DIR": "audit_logs",
    }
    base.update(kwargs)
    return Settings(**base)


def test_resolve_lakebase_default():
    s = _settings(PGHOST="host", PGDATABASE="app_ontos", ENV="PROD")
    assert resolve_storage_mode(s) == StorageMode.LAKEBASE


def test_resolve_postgres_password_mode():
    s = _settings(
        PGHOST="pg.example.com",
        PGDATABASE="app_ontos",
        PGUSER="u",
        PGPASSWORD="p",
        DB_USE_PASSWORD_AUTH=True,
        ENV="PROD",
    )
    assert resolve_storage_mode(s) == StorageMode.POSTGRES
    assert uses_lakebase_oauth(s) is False


def test_resolve_uc_readonly_explicit():
    s = _settings(STORAGE_MODE="uc_readonly", ENV="PROD")
    assert resolve_storage_mode(s) == StorageMode.UC_READONLY
    assert requires_oltp_database(StorageMode.UC_READONLY) is False


def test_resolve_uc_native_explicit():
    s = _settings(STORAGE_MODE="uc_native", ENV="PROD")
    assert resolve_storage_mode(s) == StorageMode.UC_NATIVE
    assert uses_uc_native_storage(StorageMode.UC_NATIVE) is True
    assert requires_oltp_database(StorageMode.UC_NATIVE) is False


def test_resolve_uc_native_default_without_postgres():
    s = _settings(ENV="PROD", PGHOST=None, PGDATABASE=None, DATABASE_URL=None)
    assert resolve_storage_mode(s) == StorageMode.UC_NATIVE


def test_capabilities_uc_readonly():
    caps = get_storage_capabilities(StorageMode.UC_READONLY, oltp_configured=False, uc_configured=True)
    assert caps["writes_enabled"] is False
    assert "crud_entities" in caps["unavailable_capabilities"]
    assert "catalog_commander" in caps["available_capabilities"]


def test_capabilities_uc_native():
    caps = get_storage_capabilities(StorageMode.UC_NATIVE, oltp_configured=False, uc_configured=True)
    assert caps["writes_enabled"] is True
    assert caps["uc_native_sor"] is True
    assert "crud_entities" in caps["available_capabilities"]
    assert "demo_seed_delta" in caps["available_capabilities"]
    assert caps["oltp_configured"] is False
