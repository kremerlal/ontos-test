"""UC-native entity relationships manager backed by Delta overlays."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING
from uuid import UUID

from src.common.errors import ConflictError, NotFoundError
from src.common.logging import get_logger
from src.models.entity_relationships import (
    EntityRelationshipCreate,
    EntityRelationshipRead,
    EntityRelationshipSummary,
    HierarchyRootGroup,
    InstanceHierarchyNode,
    LineageGraph,
    LineageGraphEdge,
    LineageGraphNode,
)

if TYPE_CHECKING:
    from src.common.uc_native.overlays import UcNativeOverlayStore
    from src.controller.ontology_schema_manager import OntologySchemaManager

logger = get_logger(__name__)

ONTOS_NS = "http://ontos.app/ontology#"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return _utcnow()


def _parse_props(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


class UcNativeEntityRelationshipsManager:
    """EntityRelationshipsManager-compatible API over UC Delta overlays."""

    def __init__(
        self,
        overlays: "UcNativeOverlayStore",
        *,
        ontology_schema_manager: Optional["OntologySchemaManager"] = None,
        assets_manager: Any = None,
        entities: Any = None,
    ) -> None:
        self._overlays = overlays
        self._osm = ontology_schema_manager
        self._assets = assets_manager
        self._entities = entities
        logger.info("UcNativeEntityRelationshipsManager initialized")

    # ------------------------------------------------------------------
    # Ontology helpers
    # ------------------------------------------------------------------

    def _normalize_relationship_type(self, relationship_type: str) -> str:
        if relationship_type.startswith("http://") or relationship_type.startswith("https://"):
            return relationship_type
        return f"{ONTOS_NS}{relationship_type}"

    def _normalize_entity_type(self, entity_type: str) -> str:
        if entity_type.startswith("http://") or entity_type.startswith("https://"):
            return entity_type
        return f"{ONTOS_NS}{entity_type}"

    def _validate_relationship(
        self, source_type: str, target_type: str, relationship_type: str
    ) -> Optional[str]:
        if self._osm is None:
            return relationship_type
        source_iri = self._normalize_entity_type(source_type)
        rel_iri = self._normalize_relationship_type(relationship_type)
        rels = self._osm.get_relationships(source_iri)
        for r in rels.outgoing:
            if r.property_iri == rel_iri:
                target_iri = self._normalize_entity_type(target_type)
                if r.target_type_iri == target_iri:
                    return r.label
                try:
                    from rdflib import URIRef, RDFS

                    for ancestor in self._osm._graph.objects(URIRef(target_iri), RDFS.subClassOf):
                        if r.target_type_iri == str(ancestor):
                            return r.label
                except Exception:
                    pass
        # Soft-allow in UC-native when ontology is incomplete; still persist.
        logger.debug(
            "Ontology did not validate %s --[%s]--> %s; allowing for UC-native",
            source_type,
            relationship_type,
            target_type,
        )
        return relationship_type

    def _relationship_label(self, relationship_type: str) -> str:
        if self._osm is None:
            return relationship_type
        try:
            from rdflib import URIRef, RDFS

            rel_iri = self._normalize_relationship_type(relationship_type)
            label = self._osm._graph.value(URIRef(rel_iri), RDFS.label)
            return str(label) if label else relationship_type
        except Exception:
            return relationship_type

    # ------------------------------------------------------------------
    # Overlay row helpers
    # ------------------------------------------------------------------

    def _list_rows(self, *, limit: int = 5000) -> List[Dict[str, Any]]:
        store = getattr(self._overlays, "_store", None)
        if store is None:
            return []
        fqn = store.table_fqn("entity_relationships")
        return store.query(
            f"SELECT * FROM {fqn} ORDER BY updated_at DESC LIMIT {int(limit)}"
        )

    def _row_source_type(self, row: Dict[str, Any]) -> str:
        props = _parse_props(row.get("snapshot_json")) or {}
        return (
            props.get("source_type")
            or props.get("source_entity_type")
            or row.get("source_entity_type")
            or "asset"
        )

    def _row_target_type(self, row: Dict[str, Any]) -> str:
        props = _parse_props(row.get("snapshot_json")) or {}
        return (
            props.get("target_type")
            or props.get("target_entity_type")
            or row.get("target_entity_type")
            or "asset"
        )

    def _types_compatible(self, requested: str, stored: str) -> bool:
        if not requested or not stored:
            return True
        if requested == stored:
            return True
        # Schema import historically wrote "asset"; asset detail pages query Catalog/Table/…
        if stored.lower() == "asset" or requested.lower() == "asset":
            return True
        return False

    def _resolve_name(self, entity_type: str, entity_id: str) -> Optional[str]:
        if self._assets is not None:
            try:
                doc = self._assets.get_asset_doc(str(entity_id))
                if doc and doc.get("name"):
                    return doc["name"]
            except Exception:
                pass
        if self._entities is not None:
            for table in ("assets", "data_products", "data_contracts", "data_domains"):
                try:
                    doc = self._entities.get_entity(table, str(entity_id))
                    if doc and doc.get("name"):
                        return doc["name"]
                except Exception:
                    continue
        return None

    def _to_read(self, row: Dict[str, Any]) -> EntityRelationshipRead:
        props = _parse_props(row.get("snapshot_json")) or {}
        # Drop internal type hints from public properties
        public_props = {
            k: v
            for k, v in props.items()
            if k
            not in {
                "source_type",
                "target_type",
                "source_entity_type",
                "target_entity_type",
                "created_by",
            }
        } or None
        source_type = self._row_source_type(row)
        target_type = self._row_target_type(row)
        source_id = str(row.get("source_entity_id") or "")
        target_id = str(row.get("target_entity_id") or "")
        return EntityRelationshipRead(
            id=UUID(str(row["id"])),
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relationship_type=str(row.get("relationship_type") or ""),
            properties=public_props,
            created_by=props.get("created_by"),
            created_at=_parse_dt(row.get("updated_at") or props.get("created_at")),
            source_name=self._resolve_name(source_type, source_id),
            target_name=self._resolve_name(target_type, target_id),
            relationship_label=self._relationship_label(str(row.get("relationship_type") or "")),
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_relationship(
        self,
        db=None,
        *,
        rel_in: EntityRelationshipCreate,
        current_user_id: str,
    ) -> EntityRelationshipRead:
        rel_label = self._validate_relationship(
            rel_in.source_type, rel_in.target_type, rel_in.relationship_type
        )
        for row in self._list_rows():
            if (
                str(row.get("source_entity_id")) == rel_in.source_id
                and str(row.get("target_entity_id")) == rel_in.target_id
                and str(row.get("relationship_type")) == rel_in.relationship_type
                and self._types_compatible(rel_in.source_type, self._row_source_type(row))
                and self._types_compatible(rel_in.target_type, self._row_target_type(row))
            ):
                raise ConflictError(
                    f"Relationship already exists: {rel_in.source_type}:{rel_in.source_id} "
                    f"--[{rel_in.relationship_type}]--> "
                    f"{rel_in.target_type}:{rel_in.target_id}"
                )

        props = dict(rel_in.properties or {})
        props["source_type"] = rel_in.source_type
        props["target_type"] = rel_in.target_type
        props["created_by"] = current_user_id
        saved = self._overlays.add_relationship(
            source_entity_id=rel_in.source_id,
            source_entity_type=rel_in.source_type,
            target_entity_id=rel_in.target_id,
            target_entity_type=rel_in.target_type,
            relationship_type=rel_in.relationship_type,
            properties=props,
        )
        read = self._to_read(saved)
        if rel_label:
            read.relationship_label = rel_label
        return read

    def delete_relationship(self, db=None, rel_id: UUID = None) -> None:
        if rel_id is None:
            raise NotFoundError("Entity relationship not found")
        store = getattr(self._overlays, "_store", None)
        if store is None:
            raise NotFoundError(f"Entity relationship not found: {rel_id}")
        existing = store.get_by_id("entity_relationships", str(rel_id))
        if not existing:
            raise NotFoundError(f"Entity relationship not found: {rel_id}")
        store.delete_by_id("entity_relationships", str(rel_id))

    def get_relationship(self, db=None, rel_id: UUID = None) -> EntityRelationshipRead:
        store = getattr(self._overlays, "_store", None)
        if store is None or rel_id is None:
            raise NotFoundError(f"Entity relationship not found: {rel_id}")
        row = store.get_by_id("entity_relationships", str(rel_id))
        if not row:
            raise NotFoundError(f"Entity relationship not found: {rel_id}")
        return self._to_read(row)

    def get_outgoing(
        self,
        db=None,
        source_type: str = "",
        source_id: str = "",
        relationship_type: Optional[str] = None,
    ) -> List[EntityRelationshipRead]:
        results = []
        for row in self._list_rows():
            if str(row.get("source_entity_id")) != source_id:
                continue
            if not self._types_compatible(source_type, self._row_source_type(row)):
                continue
            if relationship_type and str(row.get("relationship_type")) != relationship_type:
                continue
            results.append(self._to_read(row))
        return results

    def get_incoming(
        self,
        db=None,
        target_type: str = "",
        target_id: str = "",
        relationship_type: Optional[str] = None,
    ) -> List[EntityRelationshipRead]:
        results = []
        for row in self._list_rows():
            if str(row.get("target_entity_id")) != target_id:
                continue
            if not self._types_compatible(target_type, self._row_target_type(row)):
                continue
            if relationship_type and str(row.get("relationship_type")) != relationship_type:
                continue
            results.append(self._to_read(row))
        return results

    def get_all_for_entity(
        self, db=None, entity_type: str = "", entity_id: str = ""
    ) -> EntityRelationshipSummary:
        outgoing: List[EntityRelationshipRead] = []
        incoming: List[EntityRelationshipRead] = []
        for row in self._list_rows():
            source_id = str(row.get("source_entity_id") or "")
            target_id = str(row.get("target_entity_id") or "")
            if source_id == entity_id and self._types_compatible(
                entity_type, self._row_source_type(row)
            ):
                outgoing.append(self._to_read(row))
            elif target_id == entity_id and self._types_compatible(
                entity_type, self._row_target_type(row)
            ):
                incoming.append(self._to_read(row))
        return EntityRelationshipSummary(
            entity_type=entity_type,
            entity_id=entity_id,
            outgoing=outgoing,
            incoming=incoming,
            total=len(outgoing) + len(incoming),
        )

    def query_relationships(
        self,
        db=None,
        *,
        source_type: Optional[str] = None,
        source_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        relationship_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[EntityRelationshipRead]:
        results: List[EntityRelationshipRead] = []
        for row in self._list_rows():
            if source_id and str(row.get("source_entity_id")) != source_id:
                continue
            if target_id and str(row.get("target_entity_id")) != target_id:
                continue
            if relationship_type and str(row.get("relationship_type")) != relationship_type:
                continue
            if source_type and not self._types_compatible(source_type, self._row_source_type(row)):
                continue
            if target_type and not self._types_compatible(target_type, self._row_target_type(row)):
                continue
            results.append(self._to_read(row))
        return results[skip : skip + limit]

    # ------------------------------------------------------------------
    # Hierarchy / lineage
    # ------------------------------------------------------------------

    def get_entity_hierarchy(
        self,
        db=None,
        entity_type: str = "",
        entity_id: str = "",
        max_depth: int = 5,
    ) -> Optional[InstanceHierarchyNode]:
        name = self._resolve_name(entity_type, entity_id) or entity_id
        root = InstanceHierarchyNode(
            entity_type=entity_type,
            entity_id=entity_id,
            name=name,
        )
        visited: Set[tuple] = set()
        self._expand_children(root, current_depth=0, max_depth=max_depth, visited=visited)
        return root

    def _expand_children(
        self,
        node: InstanceHierarchyNode,
        *,
        current_depth: int,
        max_depth: int,
        visited: Set[tuple],
    ) -> None:
        if current_depth >= max_depth:
            return
        key = (node.entity_type, node.entity_id)
        if key in visited:
            return
        visited.add(key)

        children: List[InstanceHierarchyNode] = []
        for rel in self.get_outgoing(source_type=node.entity_type, source_id=node.entity_id):
            child = InstanceHierarchyNode(
                entity_type=rel.target_type,
                entity_id=rel.target_id,
                name=rel.target_name or rel.target_id,
                relationship_type=rel.relationship_type,
                relationship_label=rel.relationship_label,
            )
            self._expand_children(
                child,
                current_depth=current_depth + 1,
                max_depth=max_depth,
                visited=visited,
            )
            children.append(child)
        node.children = children
        node.child_count = len(children)

    def get_hierarchy_roots(
        self, db=None, root_types: Optional[List[str]] = None
    ) -> List[HierarchyRootGroup]:
        wanted = root_types or ["System", "DataDomain"]
        groups: Dict[str, HierarchyRootGroup] = {
            t: HierarchyRootGroup(entity_type=t, label=t, roots=[]) for t in wanted
        }
        # Prefer assets of those types when assets manager is available.
        if self._assets is not None:
            try:
                for doc in self._assets.list_assets(limit=2000):
                    type_name = doc.get("asset_type_name") or "Asset"
                    if type_name not in groups:
                        continue
                    groups[type_name].roots.append(
                        InstanceHierarchyNode(
                            entity_type=type_name,
                            entity_id=str(doc["id"]),
                            name=doc.get("name") or str(doc["id"]),
                            status=doc.get("status"),
                            description=doc.get("description"),
                        )
                    )
            except Exception as exc:
                logger.debug("Failed listing hierarchy roots from assets: %s", exc)

        # Also include entities that appear as relationship sources of wanted types
        # but never as targets (approx roots).
        seen_ids = {
            (g.entity_type, r.entity_id) for g in groups.values() for r in g.roots
        }
        target_ids = {str(r.get("target_entity_id")) for r in self._list_rows()}
        for row in self._list_rows():
            source_type = self._row_source_type(row)
            source_id = str(row.get("source_entity_id") or "")
            if source_type not in groups:
                continue
            if source_id in target_ids:
                continue
            key = (source_type, source_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            groups[source_type].roots.append(
                InstanceHierarchyNode(
                    entity_type=source_type,
                    entity_id=source_id,
                    name=self._resolve_name(source_type, source_id) or source_id,
                )
            )
        return [g for g in groups.values() if g.roots]

    def get_business_lineage(
        self, db=None, entity_type: str = "", entity_id: str = "", **_
    ) -> LineageGraph:
        summary = self.get_all_for_entity(entity_type=entity_type, entity_id=entity_id)
        center_key = f"{entity_type}:{entity_id}"
        nodes: Dict[str, LineageGraphNode] = {
            center_key: LineageGraphNode(
                id=center_key,
                entity_type=entity_type,
                entity_id=entity_id,
                name=self._resolve_name(entity_type, entity_id) or entity_id,
                is_center=True,
            )
        }
        edges: List[LineageGraphEdge] = []
        for rel in summary.outgoing + summary.incoming:
            for etype, eid, name in (
                (rel.source_type, rel.source_id, rel.source_name),
                (rel.target_type, rel.target_id, rel.target_name),
            ):
                key = f"{etype}:{eid}"
                if key not in nodes:
                    nodes[key] = LineageGraphNode(
                        id=key,
                        entity_type=etype,
                        entity_id=eid,
                        name=name or eid,
                    )
            edges.append(
                LineageGraphEdge(
                    source=f"{rel.source_type}:{rel.source_id}",
                    target=f"{rel.target_type}:{rel.target_id}",
                    relationship_type=rel.relationship_type,
                    label=rel.relationship_label,
                )
            )
        return LineageGraph(
            center_entity_type=entity_type,
            center_entity_id=entity_id,
            nodes=list(nodes.values()),
            edges=edges,
        )
