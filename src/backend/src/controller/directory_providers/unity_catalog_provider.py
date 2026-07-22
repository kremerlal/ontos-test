"""Directory provider backed by a Unity Catalog Delta table."""

from __future__ import annotations

from typing import List

from src.common.unity_catalog_utils import sanitize_uc_identifier
from src.controller.directory_providers.base import (
    DirectoryError,
    DirectoryProvider,
    DirectoryProviderConfig,
    DirectoryProviderContext,
)
from src.models.directory import Principal, PrincipalType


def _validate_fqn(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 3:
        raise DirectoryError(
            "Unity Catalog directory table must be catalog.schema.table"
        )
    try:
        return ".".join(sanitize_uc_identifier(part) for part in parts)
    except Exception as exc:
        raise DirectoryError("Invalid Unity Catalog directory table name") from exc


def _literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''").replace("%", "\\%").replace("_", "\\_")


class UnityCatalogProvider(DirectoryProvider):
    """Read principals from a UC table through Statement Execution."""

    def __init__(
        self,
        context: DirectoryProviderContext,
        config: DirectoryProviderConfig,
    ) -> None:
        if context.ws_client is None:
            raise DirectoryError("Workspace client is required for UC directory")
        if not context.warehouse_id:
            raise DirectoryError("SQL warehouse is required for UC directory")
        self._ws = context.ws_client
        self._warehouse_id = context.warehouse_id
        self._table = _validate_fqn(config.uc_table or "")

    def _query(self, principal_type: str, prefix: str, top: int) -> List[Principal]:
        limit = max(1, min(int(top), 100))
        escaped = _literal(prefix.strip().lower())
        statement = (
            "SELECT type, id, display_name, sub_label "
            f"FROM {self._table} "
            f"WHERE type = '{principal_type}' "
            "AND ("
            f"lower(display_name) LIKE '{escaped}%' ESCAPE '\\\\' "
            f"OR lower(id) LIKE '{escaped}%' ESCAPE '\\\\'"
            f") ORDER BY display_name LIMIT {limit}"
        )
        try:
            response = self._ws.statement_execution.execute_statement(
                warehouse_id=self._warehouse_id,
                statement=statement,
                wait_timeout="30s",
            )
            status = getattr(response, "status", None)
            error = getattr(status, "error", None)
            if error:
                raise DirectoryError(
                    getattr(error, "message", None) or "UC directory query failed"
                )
            data = getattr(getattr(response, "result", None), "data_array", None) or []
        except DirectoryError:
            raise
        except Exception as exc:
            raise DirectoryError(f"UC directory query failed: {exc}") from exc
        return [
            Principal(
                type=PrincipalType(row[0]),
                id=str(row[1]),
                display_name=str(row[2]),
                sub_label=str(row[3]) if len(row) > 3 and row[3] is not None else None,
            )
            for row in data
        ]

    def search_users(self, prefix: str, top: int) -> List[Principal]:
        return self._query(PrincipalType.USER.value, prefix, top)

    def search_groups(self, prefix: str, top: int) -> List[Principal]:
        return self._query(PrincipalType.GROUP.value, prefix, top)

    def _get(self, principal_type: str, identifier: str) -> Principal:
        escaped = identifier.replace("'", "''")
        statement = (
            "SELECT type, id, display_name, sub_label "
            f"FROM {self._table} WHERE type = '{principal_type}' "
            f"AND id = '{escaped}' LIMIT 1"
        )
        try:
            response = self._ws.statement_execution.execute_statement(
                warehouse_id=self._warehouse_id,
                statement=statement,
                wait_timeout="30s",
            )
            data = getattr(getattr(response, "result", None), "data_array", None) or []
        except Exception as exc:
            raise DirectoryError(f"UC directory lookup failed: {exc}") from exc
        if not data:
            raise DirectoryError(f"{principal_type.title()} not found: {identifier}")
        row = data[0]
        return Principal(
            type=PrincipalType(row[0]),
            id=str(row[1]),
            display_name=str(row[2]),
            sub_label=str(row[3]) if len(row) > 3 and row[3] is not None else None,
        )

    def get_user(self, id: str) -> Principal:
        return self._get(PrincipalType.USER.value, id)

    def get_group(self, id: str) -> Principal:
        return self._get(PrincipalType.GROUP.value, id)

    def test(self) -> None:
        try:
            response = self._ws.statement_execution.execute_statement(
                warehouse_id=self._warehouse_id,
                statement=(
                    "SELECT type, id, display_name, sub_label "
                    f"FROM {self._table} LIMIT 1"
                ),
                wait_timeout="30s",
            )
            error = getattr(getattr(response, "status", None), "error", None)
            if error:
                raise DirectoryError(
                    getattr(error, "message", None) or "UC directory test failed"
                )
        except DirectoryError:
            raise
        except Exception as exc:
            raise DirectoryError(f"UC directory test failed: {exc}") from exc
