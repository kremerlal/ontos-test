"""UC-native bulk asset import/export — Delta-backed equivalent of AssetBulkManager."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from src.common.logging import get_logger
from src.common.uc_native.managers import UcNativeAssetsManager
from src.controller.asset_bulk_manager import (
    BULK_IMPORT_MAX_ROWS,
    IMPORT_COLUMNS,
    MAX_RELATIONSHIP_DEPTH,
    AssetBulkManager,
    ImportAction,
    ImportPreviewItem,
    ImportPreviewResult,
    ImportResult,
    ImportResultItem,
    _format_properties,
    _format_tags,
    _parse_properties,
    _parse_tags,
)
from src.models.assets import (
    AssetCreate,
    AssetRelationshipCreate,
    AssetStatus,
    AssetUpdate,
)

logger = get_logger(__name__)


class UcNativeAssetBulkManager:
    """CSV/XLSX import-export against ``UcNativeAssetsManager``.

    Mirrors the public surface of ``AssetBulkManager`` so the bulk routes can
    swap implementations without changes. File parsing and shared validation
    helpers are reused from the Postgres manager module.
    """

    def __init__(self, assets_manager: UcNativeAssetsManager) -> None:
        self._assets = assets_manager
        # Reuse CSV/XLSX parse + serialise helpers without constructing a
        # Postgres-backed manager (its __init__ only sets repos).
        self._io = AssetBulkManager.__new__(AssetBulkManager)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_assets(
        self,
        db=None,
        *,
        fmt: str = "csv",
        asset_ids: Optional[List[UUID]] = None,
        asset_type_id: Optional[UUID] = None,
        platform: Optional[str] = None,
        domain_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[bytes, str, str]:
        if asset_ids:
            docs = []
            for aid in asset_ids:
                doc = self._assets.get_asset_doc(str(aid))
                if doc:
                    docs.append(doc)
        else:
            page = self._assets.get_all_assets(
                db=db,
                skip=0,
                limit=BULK_IMPORT_MAX_ROWS,
                asset_type_id=asset_type_id,
                platform=platform,
                domain_id=domain_id,
                status=status,
            )
            docs = [self._assets.get_asset_doc(str(item.id)) or {} for item in page.items]

        parents = self._assets._parent_index()
        parent_rels = self._parent_relationship_types()
        name_by_id = {
            str(d.get("id")): d.get("name")
            for d in self._assets.list_assets(limit=BULK_IMPORT_MAX_ROWS)
            if d.get("id")
        }
        rows = [
            self._doc_to_row(doc, parents=parents, parent_rels=parent_rels, names=name_by_id)
            for doc in docs
            if doc.get("id")
        ]

        if fmt == "xlsx":
            return (
                self._io._rows_to_xlsx(rows),
                "assets-export.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        return self._io._rows_to_csv(rows), "assets-export.csv", "text/csv"

    def export_template(
        self,
        db=None,
        *,
        asset_type_name: Optional[str] = None,
        fmt: str = "csv",
    ) -> Tuple[bytes, str, str]:
        example = {col: "" for col in IMPORT_COLUMNS}
        example["name"] = "Example Asset"
        example["status"] = "draft"

        if asset_type_name:
            example["asset_type"] = asset_type_name
            at = self._assets.get_asset_type_by_name(asset_type_name)
            if at:
                doc = self._assets._asset_type_doc(str(at.id)) or {}
                required = doc.get("required_fields") or {}
                if required:
                    props = {}
                    for field_name, field_def in required.items():
                        if isinstance(field_def, dict):
                            props[field_name] = f"<{field_def.get('type', 'value')}>"
                        else:
                            props[field_name] = "<value>"
                    example["properties"] = json.dumps(props, ensure_ascii=False)
        else:
            example["asset_type"] = "Table"

        rows = [example]
        if fmt == "xlsx":
            return (
                self._io._rows_to_xlsx(rows, columns=IMPORT_COLUMNS),
                "assets-template.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        return (
            self._io._rows_to_csv(rows, columns=IMPORT_COLUMNS),
            "assets-template.csv",
            "text/csv",
        )

    def _doc_to_row(
        self,
        doc: Dict[str, Any],
        *,
        parents: Dict[str, str],
        parent_rels: Dict[str, str],
        names: Dict[str, Optional[str]],
    ) -> Dict[str, str]:
        asset_id = str(doc.get("id") or "")
        parent_id = parents.get(asset_id, "")
        return {
            "id": asset_id,
            "name": doc.get("name") or "",
            "asset_type": doc.get("asset_type_name") or "",
            "description": doc.get("description") or "",
            "platform": doc.get("platform") or "",
            "location": doc.get("location") or "",
            "domain_id": doc.get("domain_id") or "",
            "status": doc.get("status") or "",
            "tags": _format_tags(doc.get("tags")),
            "properties": _format_properties(doc.get("properties")),
            "parent_asset": names.get(parent_id) or "",
            "parent_relationship_type": parent_rels.get(asset_id, ""),
            "created_by": doc.get("created_by") or "",
            "created_at": str(doc.get("created_at") or ""),
            "updated_at": str(doc.get("updated_at") or ""),
        }

    def _parent_relationship_types(self) -> Dict[str, str]:
        """child asset id -> relationship_type from hierarchical overlays."""
        out: Dict[str, str] = {}
        for parent_id, children in self._assets._hierarchy_index().items():
            for child_id, rel_type in children:
                out[child_id] = rel_type
        return out

    # ------------------------------------------------------------------
    # Import - Preview / Execute
    # ------------------------------------------------------------------

    def _resolve_asset_types(self) -> Dict[str, UUID]:
        return {
            t.name.lower(): t.id
            for t in self._assets.get_all_asset_types(db=None, skip=0, limit=10000)
            if t.name
        }

    def preview_import(self, db=None, *, file_bytes: bytes, filename: str) -> ImportPreviewResult:
        result = ImportPreviewResult()
        rows = self._io._parse_file(file_bytes, filename)
        result.total_rows = len(rows)

        if len(rows) == 0:
            result.error_messages.append("File is empty or has no data rows.")
            return result
        if len(rows) > BULK_IMPORT_MAX_ROWS:
            result.error_messages.append(
                f"File contains {len(rows)} rows, maximum is {BULK_IMPORT_MAX_ROWS}. Split into multiple files."
            )
            return result

        type_map = self._resolve_asset_types()
        dup_first_rows = self._io._detect_duplicates(rows)
        dup_keys = set()
        for idx, row in enumerate(rows):
            key = "|".join(
                [
                    row.get("name", "").lower(),
                    row.get("asset_type", "").lower(),
                    row.get("platform", "").lower(),
                    row.get("location", "").lower(),
                ]
            )
            if key in dup_first_rows and (idx + 1) != dup_first_rows[key]:
                dup_keys.add((idx, key))

        cycles = self._io._detect_parent_cycles(rows)
        if cycles:
            result.error_messages.append(
                f"Circular parent references detected: {'; '.join(cycles)}"
            )

        for idx, row in enumerate(rows):
            row_num = idx + 1
            name = row.get("name", "").strip()
            asset_type_raw = row.get("asset_type", "").strip()
            item = ImportPreviewItem(
                row=row_num, name=name, asset_type=asset_type_raw, action=ImportAction.ERROR
            )

            if not name:
                item.message = "Missing required field: name"
                result.errors += 1
                result.items.append(item)
                continue
            if not asset_type_raw:
                item.message = "Missing required field: asset_type"
                result.errors += 1
                result.items.append(item)
                continue

            type_id = type_map.get(asset_type_raw.lower())
            if not type_id:
                item.message = f"Unknown asset type: '{asset_type_raw}'"
                result.errors += 1
                result.items.append(item)
                continue

            dup_key = "|".join(
                [
                    name.lower(),
                    asset_type_raw.lower(),
                    row.get("platform", "").strip().lower(),
                    row.get("location", "").strip().lower(),
                ]
            )
            if (idx, dup_key) in dup_keys:
                item.message = f"Duplicate of row {dup_first_rows[dup_key]} in this file"
                item.action = ImportAction.ERROR
                result.errors += 1
                result.items.append(item)
                continue

            status_raw = row.get("status", "").strip().lower()
            if status_raw and status_raw not in [s.value for s in AssetStatus]:
                item.message = (
                    f"Invalid status: '{status_raw}'. Must be one of: "
                    f"{', '.join(s.value for s in AssetStatus)}"
                )
                result.errors += 1
                result.items.append(item)
                continue

            try:
                _parse_properties(row.get("properties", ""))
            except ValueError as e:
                item.message = str(e)
                result.errors += 1
                result.items.append(item)
                continue

            row_id = row.get("id", "").strip()
            if row_id:
                try:
                    existing = self._assets.get_asset_doc(str(UUID(row_id)))
                except (ValueError, Exception):
                    existing = None
                if existing:
                    item.action = ImportAction.UPDATE
                    item.existing_asset_id = str(existing["id"])
                    result.will_update += 1
                else:
                    item.message = f"Asset with ID '{row_id}' not found (stale ID)"
                    result.errors += 1
                    result.items.append(item)
                    continue
            else:
                platform_val = row.get("platform", "").strip() or None
                location_val = row.get("location", "").strip() or None
                existing_ns = self._assets.get_by_identity(
                    name=name,
                    asset_type_id=type_id,
                    platform=platform_val,
                    location=location_val,
                )
                if existing_ns:
                    item.action = ImportAction.UPDATE
                    item.existing_asset_id = str(existing_ns.id)
                    result.will_update += 1
                else:
                    item.action = ImportAction.CREATE
                    result.will_create += 1

            result.items.append(item)

        return result

    def execute_import(
        self,
        db=None,
        *,
        file_bytes: bytes,
        filename: str,
        current_user_id: str,
    ) -> ImportResult:
        result = ImportResult()
        rows = self._io._parse_file(file_bytes, filename)
        if not rows:
            result.error_messages.append("File is empty or has no data rows.")
            return result
        if len(rows) > BULK_IMPORT_MAX_ROWS:
            result.error_messages.append(
                f"File contains {len(rows)} rows, maximum is {BULK_IMPORT_MAX_ROWS}. Split into multiple files."
            )
            return result

        type_map = self._resolve_asset_types()
        dup_first_rows = self._io._detect_duplicates(rows)
        cycles = self._io._detect_parent_cycles(rows)
        has_cycles = len(cycles) > 0
        created_assets: Dict[str, UUID] = {}

        for idx, row in enumerate(rows):
            row_num = idx + 1
            name = row.get("name", "").strip()
            asset_type_raw = row.get("asset_type", "").strip()
            item = ImportResultItem(
                row=row_num, name=name, asset_type=asset_type_raw, action=ImportAction.ERROR
            )

            if not name or not asset_type_raw:
                item.message = f"Missing required field: {'name' if not name else 'asset_type'}"
                result.errors += 1
                result.items.append(item)
                continue

            type_id = type_map.get(asset_type_raw.lower())
            if not type_id:
                item.message = f"Unknown asset type: '{asset_type_raw}'"
                result.errors += 1
                result.items.append(item)
                continue

            dup_key = "|".join(
                [
                    name.lower(),
                    asset_type_raw.lower(),
                    row.get("platform", "").strip().lower(),
                    row.get("location", "").strip().lower(),
                ]
            )
            if dup_key in dup_first_rows and (idx + 1) != dup_first_rows[dup_key]:
                item.message = f"Duplicate of row {dup_first_rows[dup_key]} in this file"
                result.errors += 1
                result.items.append(item)
                continue

            status_raw = row.get("status", "").strip().lower()
            if status_raw and status_raw not in [s.value for s in AssetStatus]:
                item.message = f"Invalid status: '{status_raw}'"
                result.errors += 1
                result.items.append(item)
                continue

            try:
                properties = _parse_properties(row.get("properties", ""))
            except ValueError as e:
                item.message = str(e)
                result.errors += 1
                result.items.append(item)
                continue

            tags = _parse_tags(row.get("tags", ""))
            description = row.get("description", "").strip() or None
            platform_val = row.get("platform", "").strip() or None
            location_val = row.get("location", "").strip() or None
            domain_id_val = row.get("domain_id", "").strip() or None
            status_val = AssetStatus(status_raw) if status_raw else AssetStatus.ACTIVE

            try:
                row_id = row.get("id", "").strip()
                if row_id:
                    try:
                        existing = self._assets.get_asset_doc(str(UUID(row_id)))
                    except (ValueError, Exception):
                        existing = None
                    if not existing:
                        item.message = f"Asset with ID '{row_id}' not found"
                        result.errors += 1
                        result.items.append(item)
                        continue
                    updated = self._assets.update_asset(
                        None,
                        asset_id=UUID(str(existing["id"])),
                        asset_in=AssetUpdate(
                            name=name,
                            description=description,
                            asset_type_id=type_id,
                            platform=platform_val,
                            location=location_val,
                            domain_id=domain_id_val,
                            properties=properties,
                            tags=tags,
                            status=status_val,
                        ),
                        current_user_id=current_user_id,
                    )
                    item.action = ImportAction.UPDATE
                    item.asset_id = str(updated.id)
                    result.updated += 1
                    created_assets[name.lower()] = updated.id
                else:
                    existing_ns = self._assets.get_by_identity(
                        name=name,
                        asset_type_id=type_id,
                        platform=platform_val,
                        location=location_val,
                    )
                    if existing_ns:
                        updated = self._assets.update_asset(
                            None,
                            asset_id=existing_ns.id,
                            asset_in=AssetUpdate(
                                description=description,
                                domain_id=domain_id_val,
                                properties=properties,
                                tags=tags,
                                status=status_val,
                            ),
                            current_user_id=current_user_id,
                        )
                        item.action = ImportAction.UPDATE
                        item.asset_id = str(updated.id)
                        result.updated += 1
                        created_assets[name.lower()] = updated.id
                    else:
                        created = self._assets.create_asset(
                            None,
                            asset_in=AssetCreate(
                                name=name,
                                description=description,
                                asset_type_id=type_id,
                                platform=platform_val,
                                location=location_val,
                                domain_id=domain_id_val,
                                properties=properties,
                                tags=tags or [],
                                status=status_val,
                            ),
                            current_user_id=current_user_id,
                        )
                        item.action = ImportAction.CREATE
                        item.asset_id = str(created.id)
                        result.created += 1
                        created_assets[name.lower()] = created.id
            except Exception as e:
                item.message = f"Error: {str(e)[:200]}"
                result.errors += 1
                result.items.append(item)
                continue

            result.items.append(item)

        if not has_cycles:
            self._wire_parent_relationships(rows, created_assets)

        return result

    def _wire_parent_relationships(
        self,
        rows: List[Dict[str, str]],
        created_assets: Dict[str, UUID],
    ) -> None:
        name_index = self._name_index()
        existing_rels = self._existing_relationship_keys()

        for idx, row in enumerate(rows):
            parent_ref = row.get("parent_asset", "").strip()
            rel_type = row.get("parent_relationship_type", "").strip() or "contains"
            name = row.get("name", "").strip()
            if not parent_ref or not name:
                continue

            child_id = created_assets.get(name.lower())
            if not child_id:
                continue

            parent_id = created_assets.get(parent_ref.lower()) or name_index.get(
                parent_ref.lower()
            )
            if not parent_id:
                logger.warning(
                    "Row %s: Parent asset '%s' not found, skipping relationship.",
                    idx + 1,
                    parent_ref,
                )
                continue

            depth = self._get_ancestor_depth(parent_id)
            if depth >= MAX_RELATIONSHIP_DEPTH:
                logger.warning(
                    "Row %s: Max relationship depth (%s) exceeded for parent '%s'.",
                    idx + 1,
                    MAX_RELATIONSHIP_DEPTH,
                    parent_ref,
                )
                continue

            key = (str(parent_id), str(child_id), rel_type)
            if key in existing_rels:
                continue

            self._assets.add_relationship(
                rel_in=AssetRelationshipCreate(
                    source_asset_id=parent_id,
                    target_asset_id=child_id,
                    relationship_type=rel_type,
                ),
                current_user_id="bulk-import",
            )
            existing_rels.add(key)

    def _name_index(self) -> Dict[str, UUID]:
        index: Dict[str, UUID] = {}
        for doc in self._assets.list_assets(limit=BULK_IMPORT_MAX_ROWS):
            name = (doc.get("name") or "").strip().lower()
            if name and doc.get("id"):
                index[name] = UUID(str(doc["id"]))
        return index

    def _existing_relationship_keys(self) -> set[tuple[str, str, str]]:
        overlays = getattr(self._assets, "_overlays", None)
        if overlays is None or not hasattr(overlays, "list_relationships"):
            return set()
        keys: set[tuple[str, str, str]] = set()
        for row in overlays.list_relationships(limit=BULK_IMPORT_MAX_ROWS) or []:
            keys.add(
                (
                    str(row.get("source_entity_id") or ""),
                    str(row.get("target_entity_id") or ""),
                    str(row.get("relationship_type") or ""),
                )
            )
        return keys

    def _get_ancestor_depth(
        self, asset_id: UUID, max_depth: int = MAX_RELATIONSHIP_DEPTH
    ) -> int:
        parents = self._assets._parent_index()
        depth = 0
        current = str(asset_id)
        visited: set[str] = set()
        while depth < max_depth:
            if current in visited:
                break
            visited.add(current)
            parent = parents.get(current)
            if not parent:
                break
            current = parent
            depth += 1
        return depth
