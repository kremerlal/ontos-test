"""Tests for Unity Catalog mirror serialization and replacement semantics."""

from datetime import datetime, timezone
from types import SimpleNamespace

from src.common.uc_mirror import (
    build_asset_mirror_rows,
    build_contract_mirror_rows,
    build_product_mirror_rows,
    replace_mirror_rows,
)


class _Column:
    def __init__(self, name):
        self.name = name


class _Model:
    __table__ = SimpleNamespace(
        columns=[_Column("id"), _Column("name"), _Column("status")]
    )

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StatementExecution:
    def __init__(self):
        self.statements = []

    def execute_statement(self, **kwargs):
        self.statements.append(kwargs["statement"])
        return SimpleNamespace(status=SimpleNamespace(error=None))


def _settings():
    return SimpleNamespace(
        DATABRICKS_CATALOG="app_data",
        APP_UC_MIRROR_SCHEMA="uc_mirror",
        DATABRICKS_WAREHOUSE_ID="warehouse",
    )


def test_product_snapshot_includes_scalar_columns():
    item = _Model(
        id="p1",
        name="Product",
        status="active",
        domain="finance",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    row = build_product_mirror_rows([item])[0]
    assert row["id"] == "p1"
    assert row["domain_id"] == "finance"
    assert '"status": "active"' in row["snapshot_json"]


def test_contract_and_asset_rows_have_read_model_fields():
    contract = _Model(
        id="c1",
        name="Contract",
        status="draft",
        data_product="p1",
        updated_at=None,
    )
    asset = _Model(
        id="a1",
        name="Table",
        status="active",
        asset_type=SimpleNamespace(name="Table"),
        updated_at=None,
    )
    assert build_contract_mirror_rows([contract])[0]["product_id"] == "p1"
    assert build_asset_mirror_rows([asset])[0]["asset_type_name"] == "Table"


def test_replace_mirror_rows_deletes_then_inserts():
    statement_execution = _StatementExecution()
    ws = SimpleNamespace(statement_execution=statement_execution)
    count = replace_mirror_rows(
        ws,
        _settings(),
        "data_products",
        [
            {
                "id": "p1",
                "name": "O'Hare",
                "status": "active",
                "domain_id": None,
                "updated_at": "2026-01-01",
                "snapshot_json": "{}",
            }
        ],
    )
    assert count == 1
    assert statement_execution.statements[0].startswith(
        "DELETE FROM app_data.uc_mirror.data_products"
    )
    assert "O''Hare" in statement_execution.statements[1]
