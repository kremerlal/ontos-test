"""Semantic / MDM / DQ result storage for uc_native mode."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from databricks.sdk import WorkspaceClient

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.uc_mirror import volume_root
from src.common.uc_native.delta_store import DeltaStore

logger = get_logger(__name__)


class UcNativeSemanticStore:
    def __init__(
        self,
        store: DeltaStore,
        ws_client: WorkspaceClient,
        settings: Settings,
    ) -> None:
        self._store = store
        self._ws = ws_client
        self._settings = settings

    def save_ontology_file(self, filename: str, content: bytes) -> str:
        path = f"{volume_root(self._settings)}/ontology/{filename}"
        self._ws.files.upload(path, content, overwrite=True)
        return path

    def merge_triples(self, triples: List[Dict[str, str]]) -> int:
        if not triples:
            return 0
        rows = [
            {
                "id": str(uuid.uuid4()),
                "subject": triple.get("subject", ""),
                "predicate": triple.get("predicate", ""),
                "object": triple.get("object", ""),
                "context": triple.get("context", ""),
            }
            for triple in triples
        ]
        # Prefer bulk insert for generator/import workloads (hundreds of triples).
        if hasattr(self._store, "insert_rows"):
            self._store.insert_rows("rdf_triples", rows, chunk_size=50)
            return len(rows)
        for row in rows:
            self._store.merge_row("rdf_triples", row)
        return len(rows)

    def search_triples(self, prefix: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        safe = prefix.replace("'", "''").replace("%", "\\%")
        fqn = self._store.table_fqn("rdf_triples")
        return self._store.query(
            f"SELECT * FROM {fqn} WHERE subject LIKE '{safe}%' "
            f"OR object LIKE '{safe}%' LIMIT {int(limit)}"
        )

    def append_job_result(
        self,
        table_name: str,
        *,
        status: str,
        parent_id: str,
        results: Dict[str, Any],
    ) -> str:
        run_id = str(uuid.uuid4())
        row: Dict[str, Any] = {
            "id": run_id,
            "status": status,
            "snapshot_json": json.dumps(results, default=str),
        }
        if table_name == "mdm_match_runs":
            row["config_id"] = parent_id
        elif table_name == "compliance_runs":
            row["policy_id"] = parent_id
        else:
            row["contract_id"] = parent_id
        self._store.merge_row(table_name, row)
        return run_id
