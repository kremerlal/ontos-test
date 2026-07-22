"""Tests for the Unity Catalog directory provider."""

from types import SimpleNamespace

import pytest

from src.controller.directory_providers.base import (
    DirectoryError,
    DirectoryProviderConfig,
    DirectoryProviderContext,
)
from src.controller.directory_providers.unity_catalog_provider import (
    UnityCatalogProvider,
)


class _Statements:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def execute_statement(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status=SimpleNamespace(error=None),
            result=SimpleNamespace(data_array=self.rows),
        )


def _provider(rows=None):
    statements = _Statements(rows)
    ws = SimpleNamespace(statement_execution=statements)
    provider = UnityCatalogProvider(
        DirectoryProviderContext(ws_client=ws, warehouse_id="wh"),
        DirectoryProviderConfig(uc_table="main.directory.principals"),
    )
    return provider, statements


def test_search_users_maps_principal_rows():
    provider, statements = _provider(
        [["user", "alice@example.com", "Alice", "alice@example.com"]]
    )
    results = provider.search_users("ali", 20)
    assert results[0].id == "alice@example.com"
    assert "type = 'user'" in statements.calls[0]["statement"]


def test_invalid_table_name_rejected():
    ws = SimpleNamespace(statement_execution=_Statements())
    with pytest.raises(DirectoryError):
        UnityCatalogProvider(
            DirectoryProviderContext(ws_client=ws, warehouse_id="wh"),
            DirectoryProviderConfig(uc_table="principals"),
        )
