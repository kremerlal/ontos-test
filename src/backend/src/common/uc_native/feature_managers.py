"""Durable UC-native feature managers backed by Delta snapshots.

These adapters accept the existing route call shapes while ignoring the OLTP
session argument. Delta is the source of record in ``uc_native`` mode.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.common.uc_native.entities import UcNativeEntityStore
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.common.uc_native.workflows import UcNativeWorkflowStore
from src.common.errors import ConflictError, NotFoundError
from src.models.entity_subscriptions import (
    EntitySubscriptionRead,
    EntitySubscriptionSummary,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data(value: Any, *, exclude_unset: bool = False) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_unset=exclude_unset)
    if hasattr(value, "dict"):
        return value.dict(exclude_unset=exclude_unset)
    return dict(vars(value))


class _EntityCrud:
    table_name = ""

    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def _save(self, payload: Any, *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        doc = dict(existing or {})
        doc.update(_data(payload, exclude_unset=existing is not None))
        doc.setdefault("id", str(uuid.uuid4()))
        doc.setdefault("status", "active")
        doc.setdefault("created_at", _now())
        doc["updated_at"] = _now()
        return self._entities.save_entity(
            self.table_name,
            doc,
            index_fields={k: doc.get(k) for k in ("name", "status", "updated_at", "etag")},
        )

    def _list(self, skip: int = 0, limit: int = 100, **filters: Any) -> List[Dict[str, Any]]:
        docs = self._entities.list_entities(self.table_name, limit=skip + limit)
        for key, value in filters.items():
            if value is not None:
                docs = [doc for doc in docs if str(doc.get(key)) == str(value)]
        return docs[skip : skip + limit]

    def _get(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self._entities.get_entity(self.table_name, str(item_id))

    def _delete(self, item_id: str) -> bool:
        if not self._get(item_id):
            return False
        self._entities.delete_entity(self.table_name, str(item_id))
        return True


class UcNativeSemanticLinksManager:
    def __init__(self, overlays: UcNativeOverlayStore) -> None:
        self._overlays = overlays

    def list_for_entity(self, entity_id: str, entity_type: str, **_) -> List[Dict[str, Any]]:
        return self._overlays.list_semantic_links(entity_id=str(entity_id), entity_type=entity_type)

    def list_for_iri(self, iri: str, **_) -> List[Dict[str, Any]]:
        return self._overlays.list_semantic_links(iri=iri)

    def add(self, payload: Any, created_by: Optional[str] = None, **_) -> Dict[str, Any]:
        doc = _data(payload)
        doc.update(
            {
                "id": str(doc.get("id") or uuid.uuid4()),
                "created_by": created_by,
                "updated_at": _now(),
            }
        )
        return self._overlays.add_semantic_link(doc)

    def remove(self, link_id: str, removed_by: Optional[str] = None, **_) -> bool:
        return self._overlays.remove_semantic_link(str(link_id))


class UcNativeTeamsManager(_EntityCrud):
    table_name = "teams"

    def create_team(self, db=None, team_in=None, current_user_id=None, **kwargs):
        return self._save(team_in or kwargs)

    def get_all_teams(self, db=None, skip=0, limit=100, domain_id=None, **_):
        return self._list(skip, limit, domain_id=domain_id)

    def get_teams_summary(self, db=None, domain_id=None, **_):
        return [
            {
                "id": d["id"],
                "name": d.get("name"),
                "title": d.get("title"),
                "domain_id": d.get("domain_id"),
                "member_count": len(d.get("members", [])),
            }
            for d in self._list(0, 1000, domain_id=domain_id)
        ]

    def get_team_by_id(self, db=None, team_id=None, **_):
        return self._get(str(team_id))

    def get_teams_by_domain(self, db=None, domain_id=None, **_):
        return self._list(0, 1000, domain_id=domain_id)

    def get_standalone_teams(self, db=None, **_):
        return [d for d in self._list(0, 1000) if not d.get("domain_id")]

    def get_teams_for_user(self, db=None, user_identifier=None, user_groups=None, **_):
        groups = set(user_groups or [])
        return [
            d
            for d in self._list(0, 1000)
            if any(
                m.get("member_identifier") == user_identifier
                or m.get("member_identifier") in groups
                for m in d.get("members", [])
            )
        ]

    def update_team(self, db=None, team_id=None, team_in=None, current_user_id=None, **_):
        existing = self._get(str(team_id))
        return self._save(team_in, existing=existing) if existing else None

    def delete_team(self, db=None, team_id=None, **_):
        return self._delete(str(team_id))

    def add_team_member(self, db=None, team_id=None, member_in=None, current_user_id=None, **kwargs):
        team = self._get(str(team_id))
        if not team:
            return None
        member = _data(member_in or kwargs)
        member.setdefault("id", str(uuid.uuid4()))
        member.setdefault("added_by", current_user_id)
        team.setdefault("members", []).append(member)
        self._save(team, existing=team)
        return member

    def get_team_members(self, db=None, team_id=None, **_):
        return (self._get(str(team_id)) or {}).get("members", [])

    def update_team_member(self, db=None, team_id=None, member_id=None, member_in=None, **_):
        team = self._get(str(team_id))
        if not team:
            return None
        for member in team.get("members", []):
            if str(member.get("id")) == str(member_id):
                member.update(_data(member_in, exclude_unset=True))
                self._save(team, existing=team)
                return member
        return None

    def remove_team_member(self, db=None, team_id=None, member_identifier=None, **_):
        team = self._get(str(team_id))
        if not team:
            return False
        before = len(team.get("members", []))
        team["members"] = [
            m
            for m in team.get("members", [])
            if m.get("member_identifier") != member_identifier
            and str(m.get("id")) != str(member_identifier)
        ]
        if before != len(team["members"]):
            self._save(team, existing=team)
            return True
        return False


class UcNativeProjectsManager(_EntityCrud):
    table_name = "projects"

    def create_project(self, db=None, project_in=None, current_user_id=None, **kwargs):
        return self._save(project_in or kwargs)

    def get_all_projects(self, db=None, skip=0, limit=100, **_):
        return self._list(skip, limit)

    def get_projects_summary(self, db=None, **_):
        return self._list(0, 1000)

    def get_project_by_id(self, db=None, project_id=None, **_):
        return self._get(str(project_id))

    def update_project(self, db=None, project_id=None, project_in=None, **_):
        old = self._get(str(project_id))
        return self._save(project_in, existing=old) if old else None

    def delete_project(self, db=None, project_id=None, **_):
        return self._delete(str(project_id))

    def assign_team_to_project(self, db=None, project_id=None, team_id=None, **_):
        project = self._get(str(project_id))
        if not project:
            return None
        project.setdefault("team_ids", [])
        if str(team_id) not in project["team_ids"]:
            project["team_ids"].append(str(team_id))
        return self._save(project, existing=project)

    def remove_team_from_project(self, db=None, project_id=None, team_id=None, **_):
        project = self._get(str(project_id))
        if not project:
            return False
        project["team_ids"] = [x for x in project.get("team_ids", []) if x != str(team_id)]
        self._save(project, existing=project)
        return True

    def get_project_teams(self, db=None, project_id=None, **_):
        return (self._get(str(project_id)) or {}).get("team_ids", [])

    def get_user_projects(self, db=None, user_id=None, user_identifier=None, team_ids=None, user_groups=None, **_):
        uid = user_id or user_identifier
        teams = set(str(t) for t in (team_ids or user_groups or []))
        return [
            p
            for p in self._list(0, 1000)
            if uid in p.get("member_ids", []) or set(p.get("team_ids", [])) & teams
        ]

    def check_user_project_access(self, db=None, project_id=None, user_id=None, team_ids=None, **_):
        return any(p["id"] == str(project_id) for p in self.get_user_projects(db, user_id=user_id, team_ids=team_ids))

    def request_project_access(self, *_, **__):
        return None


class UcNativeBusinessRolesManager(_EntityCrud):
    table_name = "business_roles"

    def create_role(self, db=None, role_in=None, current_user_id=None, **kwargs):
        return self._save(role_in or kwargs)

    def get_role(self, db=None, role_id=None, **_):
        return self._get(str(role_id))

    def get_all_roles(self, db=None, skip=0, limit=100, category=None, status=None, **_):
        return self._list(skip, limit, category=category, status=status)

    def update_role(self, db=None, role_id=None, role_in=None, current_user_id=None, **_):
        old = self._get(str(role_id))
        return self._save(role_in, existing=old) if old else None

    def delete_role(self, db=None, role_id=None, **_):
        old = self._get(str(role_id))
        if not old:
            return None
        self._delete(str(role_id))
        return old


class UcNativeBusinessOwnersManager(_EntityCrud):
    table_name = "business_owners"

    def assign_owner(self, db=None, owner_in=None, current_user_id=None, **kwargs):
        return self._save(owner_in or kwargs)

    def get_owner(self, db=None, owner_id=None, **_):
        return self._get(str(owner_id))

    def get_all_owners(self, db=None, skip=0, limit=100, **filters):
        return self._list(skip, limit, **{k: v for k, v in filters.items() if k not in ("db",)})

    def get_owners_for_object(self, db=None, object_type=None, object_id=None, **_):
        return [
            o
            for o in self._list(0, 1000)
            if o.get("object_type") == object_type and str(o.get("object_id")) == str(object_id)
        ]

    def get_owner_history(self, db=None, object_type=None, object_id=None, **_):
        return self.get_owners_for_object(db, object_type=object_type, object_id=object_id)

    def get_ownerships_for_user(self, db=None, user_email=None, active_only=True, **_):
        owners = [o for o in self._list(0, 1000) if o.get("user_email") == user_email or o.get("owner_email") == user_email]
        if active_only:
            owners = [o for o in owners if o.get("status", "active") == "active"]
        return owners

    def update_owner(self, db=None, owner_id=None, owner_in=None, current_user_id=None, **_):
        old = self._get(str(owner_id))
        return self._save(owner_in, existing=old) if old else None

    def remove_owner(self, db=None, owner_id=None, removal=None, current_user_id=None, **_):
        old = self._get(str(owner_id))
        if not old:
            return None
        old.update(_data(removal))
        old["status"] = "removed"
        old["removed_by"] = current_user_id
        return self._save(old, existing=old)


class UcNativeDeliveryMethodsManager(_EntityCrud):
    table_name = "delivery_methods"

    def create(self, db=None, obj_in=None, current_user_id=None, **kwargs):
        return self._save(obj_in or kwargs)

    def get(self, db=None, obj_id=None, **_):
        return self._get(str(obj_id))

    def get_all(self, db=None, skip=0, limit=100, category=None, status=None, **_):
        return self._list(skip, limit, category=category, status=status)

    def update(self, db=None, obj_id=None, obj_in=None, current_user_id=None, **_):
        old = self._get(str(obj_id))
        return self._save(obj_in, existing=old) if old else None

    def delete(self, db=None, obj_id=None, **_):
        old = self._get(str(obj_id))
        if not old:
            return None
        self._delete(str(obj_id))
        return old


class UcNativeDataAssetReviewManager(_EntityCrud):
    table_name = "data_asset_reviews"

    def create_review_request(self, request_data=None, **kwargs):
        doc = _data(request_data or kwargs)
        doc.setdefault("assets", [])
        return self._save(doc)

    def list_review_requests(self, skip=0, limit=100, **_):
        return self._list(skip, limit)

    def get_review_request(self, request_id=None, **_):
        return self._get(str(request_id))

    def update_review_request(self, request_id=None, update_data=None, **_):
        old = self._get(str(request_id))
        return self._save(update_data, existing=old) if old else None

    def update_review_request_status(self, request_id=None, status_update=None, **_):
        return self.update_review_request(request_id, status_update)

    def update_reviewed_asset_status(self, request_id=None, asset_id=None, asset_update=None, **_):
        review = self._get(str(request_id))
        if not review:
            return None
        assets = review.setdefault("assets", [])
        for asset in assets:
            if str(asset.get("id")) == str(asset_id) or str(asset.get("asset_id")) == str(asset_id):
                asset.update(_data(asset_update, exclude_unset=True))
                self._save(review, existing=review)
                return asset
        return None

    def delete_review_request(self, request_id=None, **_):
        return self._delete(str(request_id))

    def get_reviewed_asset(self, request_id=None, asset_id=None, **_):
        review = self._get(str(request_id)) or {}
        for asset in review.get("assets", []):
            if str(asset.get("id")) == str(asset_id) or str(asset.get("asset_id")) == str(asset_id):
                return asset
        return None

    async def get_asset_definition(self, *_, **__):
        raise ValueError("Asset definition preview is unavailable in UC-native mode")

    async def get_table_preview(self, *_, **__):
        return []

    async def analyze(self, *_, **__):
        return {}


class UcNativeEntitySubscriptionsManager:
    def __init__(self, overlays: UcNativeOverlayStore) -> None:
        self._overlays = overlays

    def subscribe(self, db=None, sub_in=None, **kwargs):
        doc = _data(sub_in or kwargs)
        existing = self.get_user_subscriptions(
            db=db, subscriber_email=doc.get("subscriber_email")
        )
        if any(
            item.entity_type == doc.get("entity_type")
            and item.entity_id == str(doc.get("entity_id"))
            for item in existing
        ):
            raise ConflictError("Already subscribed to this entity")
        doc.setdefault("id", str(uuid.uuid4()))
        doc.setdefault("created_at", _now())
        return EntitySubscriptionRead.model_validate(self._overlays.subscribe(doc))

    def unsubscribe(self, db=None, subscription_id=None, **_):
        if not self._overlays.unsubscribe(str(subscription_id)):
            raise NotFoundError(f"Subscription not found: {subscription_id}")

    def get_subscribers(self, db=None, entity_type=None, entity_id=None, **_):
        subscribers = [
            EntitySubscriptionRead.model_validate(item)
            for item in self._overlays.list_for_entity(
                "entity_subscriptions", entity_type, str(entity_id)
            )
        ]
        return EntitySubscriptionSummary(
            entity_type=entity_type,
            entity_id=str(entity_id),
            subscribers=subscribers,
            total=len(subscribers),
        )

    def get_user_subscriptions(self, db=None, subscriber_email=None, **_):
        rows = self._overlays._store.list_rows("entity_subscriptions", limit=1000)
        return [
            EntitySubscriptionRead.model_validate(
                {**self._overlays._store.parse_snapshot(row), "id": row.get("id")}
            )
            for row in rows
            if row.get("subscriber_email") == subscriber_email
        ]


class _OverlayItems:
    table_name = ""

    def __init__(self, overlays: UcNativeOverlayStore) -> None:
        self._overlays = overlays

    def create(self, db=None, data=None, item_in=None, user_email=None, **kwargs):
        doc = _data(data or item_in or kwargs)
        doc.setdefault("created_by", user_email)
        return self._overlays.add(self.table_name, doc)

    def list(self, db=None, entity_type=None, entity_id=None, **_):
        return self._overlays.list_for_entity(self.table_name, entity_type, str(entity_id))

    def summarize(self, db=None, entity_type=None, entity_id=None, **_):
        return {"count": len(self.list(db, entity_type=entity_type, entity_id=entity_id))}

    def update(self, db=None, id=None, item_id=None, data=None, item_in=None, user_email=None, **_):
        key = str(id or item_id)
        old = self._overlays.get(self.table_name, key)
        if not old:
            return None
        old.update(_data(data or item_in, exclude_unset=True))
        old["id"] = key
        old["updated_by"] = user_email
        return self._overlays.add(self.table_name, old)

    def delete(self, db=None, id=None, item_id=None, user_email=None, **_):
        return self._overlays.remove(self.table_name, str(id or item_id))


class UcNativeCostsManager(_OverlayItems):
    table_name = "cost_items"


class UcNativeQualityManager(_OverlayItems):
    table_name = "quality_items"

    def aggregate_for_product(self, db=None, product_id=None, data_products_manager=None, **_):
        return {"product_id": str(product_id), "count": 0, "items": []}


class UcNativeMetadataManager(_OverlayItems):
    table_name = "metadata_items"


class UcNativeComplianceManager(_EntityCrud):
    table_name = "compliance_policies"

    def __init__(self, entities: UcNativeEntityStore, semantic_store=None) -> None:
        super().__init__(entities)
        self._semantic = semantic_store

    def get_policies_with_stats(self, db=None, **_):
        return self._list(0, 1000)

    def get_policy_with_examples(self, db=None, policy_id=None, **_):
        return self._get(str(policy_id))

    def get_policy(self, db=None, policy_id=None, **_):
        return self._get(str(policy_id))

    def create_policy(self, db=None, policy_in=None, current_user_id=None, **kwargs):
        return self._save(policy_in or kwargs)

    def update_policy(self, db=None, policy_id=None, policy_in=None, current_user_id=None, **_):
        old = self._get(str(policy_id))
        return self._save(policy_in, existing=old) if old else None

    def delete_policy(self, db=None, policy_id=None, current_user_id=None, **_):
        return self._delete(str(policy_id))

    def list_runs(self, db=None, policy_id=None, limit=50, **_):
        return []

    def list_results(self, db=None, run_id=None, only_failed=False, limit=2000, **_):
        return []

    def get_compliance_stats(self, db=None, **_):
        return {}

    def get_compliance_trend(self, db=None, **_):
        return []


class UcNativeMdmManager(_EntityCrud):
    table_name = "mdm_configs"

    def __init__(self, entities: UcNativeEntityStore, semantic_store=None) -> None:
        super().__init__(entities)
        self._semantic = semantic_store

    def list_configs(self, project_id=None, status=None, skip=0, limit=100, **_):
        return self._list(skip, limit, project_id=project_id, status=status)

    def get_config(self, config_id=None, **_):
        return self._get(str(config_id))

    def create_config(self, data=None, created_by=None, **_):
        doc = _data(data)
        doc["created_by"] = created_by
        doc.setdefault("source_links", [])
        return self._save(doc)

    def update_config(self, config_id=None, data=None, updated_by=None, **_):
        old = self._get(str(config_id))
        if not old:
            return None
        doc = _data(data)
        doc["updated_by"] = updated_by
        return self._save(doc, existing=old)

    def delete_config(self, config_id=None, **_):
        return self._delete(str(config_id))

    def list_source_links(self, config_id=None, **_):
        return (self._get(str(config_id)) or {}).get("source_links", [])

    def create_source_link(self, config_id=None, data=None, **_):
        cfg = self._get(str(config_id))
        if not cfg:
            return None
        link = _data(data)
        link.setdefault("id", str(uuid.uuid4()))
        cfg.setdefault("source_links", []).append(link)
        self._save(cfg, existing=cfg)
        return link

    def update_source_link(self, link_id=None, data=None, **_):
        for cfg in self._list(0, 1000):
            for link in cfg.get("source_links", []):
                if str(link.get("id")) == str(link_id):
                    link.update(_data(data, exclude_unset=True))
                    self._save(cfg, existing=cfg)
                    return link
        return None

    def delete_source_link(self, link_id=None, **_):
        for cfg in self._list(0, 1000):
            before = len(cfg.get("source_links", []))
            cfg["source_links"] = [l for l in cfg.get("source_links", []) if str(l.get("id")) != str(link_id)]
            if before != len(cfg["source_links"]):
                self._save(cfg, existing=cfg)
                return True
        return False

    def list_match_runs(self, config_id=None, skip=0, limit=100, **_):
        return []

    def get_match_run(self, run_id=None, **_):
        return None

    def list_match_candidates(self, run_id=None, status=None, skip=0, limit=100, **_):
        return []

    def get_match_candidate(self, candidate_id=None, **_):
        return None

    def update_match_candidate(self, *_, **__):
        return None

    def create_review_for_matches(self, *_, **__):
        return {"status": "not_supported"}

    def run_match(self, *_, **__):
        return {"status": "not_started"}


class UcNativeTermMappingManager(_EntityCrud):
    table_name = "term_mapping_runs"

    def __init__(self, entities: UcNativeEntityStore, semantic_links=None, **_) -> None:
        super().__init__(entities)
        self._links = semantic_links

    def create_run(self, db=None, payload=None, run_in=None, created_by=None, **kwargs):
        doc = _data(payload or run_in or kwargs)
        doc["created_by"] = created_by
        doc.setdefault("status", "suggested")
        return self._save(doc)

    def list_runs(self, db=None, limit=50, **_):
        return self._list(0, limit)

    def get_run(self, db=None, run_id=None, **_):
        return self._get(str(run_id))

    def list_suggestions(self, db=None, run_id=None, status=None, limit=500, offset=0, **_):
        docs = self._entities.list_entities("term_mapping_suggestions", limit=limit + offset)
        filtered = [
            x
            for x in docs
            if x.get("run_id") == str(run_id) and (status is None or x.get("status") == status)
        ]
        return filtered[offset : offset + limit]

    def decide(self, db=None, batch=None, decided_by=None, **_):
        decisions = _data(batch).get("decisions") or []
        accepted = rejected = skipped = 0
        for decision in decisions:
            d = _data(decision)
            sug = self._entities.get_entity("term_mapping_suggestions", str(d.get("suggestion_id")))
            if not sug:
                skipped += 1
                continue
            status = d.get("status") or d.get("decision") or "accepted"
            sug["status"] = status
            sug["decided_by"] = decided_by
            sug["updated_at"] = _now()
            self._entities.save_entity(
                "term_mapping_suggestions",
                sug,
                index_fields={"run_id": sug.get("run_id"), "status": status, "updated_at": sug["updated_at"]},
            )
            if status == "accepted":
                accepted += 1
            elif status == "rejected":
                rejected += 1
            else:
                skipped += 1
        return {"accepted": accepted, "rejected": rejected, "skipped": skipped}

    def apply_run(self, db=None, run_id=None, applied_by=None, **_):
        links_created = links_skipped = 0
        for sug in self.list_suggestions(db, run_id=run_id, status="accepted"):
            if not self._links:
                links_skipped += 1
                continue
            self._links.add(
                {
                    "entity_id": sug.get("source_entity_id"),
                    "entity_type": sug.get("source_entity_type"),
                    "iri": sug.get("concept_iri") or sug.get("iri"),
                    "link_type": sug.get("link_type") or "mapped_to",
                },
                created_by=applied_by,
            )
            sug["status"] = "applied"
            self._entities.save_entity(
                "term_mapping_suggestions",
                sug,
                index_fields={"run_id": sug.get("run_id"), "status": "applied", "updated_at": _now()},
            )
            links_created += 1
        run = self._get(str(run_id))
        if run:
            run["status"] = "applied"
            self._save(run, existing=run)
        return {"run_id": str(run_id), "links_created": links_created, "links_skipped": links_skipped}

    def undo_run(self, db=None, run_id=None, **_):
        run = self._get(str(run_id))
        if run:
            run["status"] = "undone"
            self._save(run, existing=run)
        return {"run_id": str(run_id), "links_removed": 0, "suggestions_reverted": 0}

    def list_suggestions_for_entity(self, db=None, entity_type=None, entity_id=None, **_):
        return [
            x
            for x in self._entities.list_entities("term_mapping_suggestions", limit=1000)
            if x.get("source_entity_type") == entity_type and str(x.get("source_entity_id")) == str(entity_id)
        ]

    def pending_count_for_entity(self, db=None, entity_type=None, entity_id=None, **_):
        pending = [
            s
            for s in self.list_suggestions_for_entity(db, entity_type=entity_type, entity_id=entity_id)
            if s.get("status") in (None, "pending", "suggested")
        ]
        return {
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "pending": len(pending),
            "auto_apply": 0,
        }

    def create_review_for_run(self, *_, **__):
        raise ValueError("Term-mapping reviews are not supported in UC-native mode")

    def suggest_inline(self, *_, **__):
        return {"suggestions": []}


class UcNativeAccessGrantsManager:
    def __init__(self, workflows: UcNativeWorkflowStore) -> None:
        self._workflows = workflows
        self._configs: Dict[str, Dict[str, Any]] = {}

    def create_request(self, db=None, requester_email=None, data=None, **_):
        d = _data(data)
        return self._workflows.create_access_grant_request(
            requester=requester_email or "",
            resource=str(d.get("entity_id") or d.get("resource") or ""),
            details=d,
        )

    def handle_request(self, db=None, request_id=None, payload=None, **_) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        row = self._workflows._store.get_by_id("access_grant_requests", str(request_id))
        if not row:
            return None, None
        doc = self._workflows._store.parse_snapshot(row)
        doc.update(_data(payload))
        status = doc.get("status", "approved")
        row.update({"status": status, "snapshot_json": json.dumps(doc, default=str), "updated_at": _now()})
        self._workflows._store.merge_row("access_grant_requests", row)
        response = {**doc, "id": str(request_id)}
        grant = response if status == "approved" else None
        return response, grant

    def get_all_pending_requests(self, db=None, limit=100, offset=0, **_):
        rows = [r for r in self._workflows._store.list_rows("access_grant_requests", limit=1000) if r.get("status") == "pending"]
        return rows[offset : offset + limit]

    def get_my_pending_requests(self, db=None, user_email=None, limit=100, offset=0, **_):
        rows = [
            r
            for r in self._workflows._store.list_rows("access_grant_requests", limit=1000)
            if r.get("requester") == user_email and r.get("status") == "pending"
        ]
        return rows[offset : offset + limit]

    def get_my_requests(self, db=None, user_email=None, limit=100, offset=0, **_):
        rows = [
            r
            for r in self._workflows._store.list_rows("access_grant_requests", limit=1000)
            if r.get("requester") == user_email
        ]
        return rows[offset : offset + limit]

    def get_my_grants(self, db=None, user_email=None, limit=100, offset=0, **_):
        rows = [
            r
            for r in self._workflows._store.list_rows("access_grant_requests", limit=1000)
            if r.get("requester") == user_email and r.get("status") == "approved"
        ]
        return rows[offset : offset + limit]

    def cancel_request(self, db=None, request_id=None, **_):
        return self.handle_request(db, request_id, {"status": "cancelled"})[0]

    def get_grants_for_entity(self, db=None, entity_type=None, entity_id=None, limit=100, offset=0, **_):
        rows = [
            r
            for r in self._workflows._store.list_rows("access_grant_requests", limit=1000)
            if r.get("resource") == str(entity_id) and r.get("status") == "approved"
        ]
        return rows[offset : offset + limit]

    def get_pending_requests_for_entity(self, db=None, entity_type=None, entity_id=None, limit=100, offset=0, **_):
        rows = [
            r
            for r in self._workflows._store.list_rows("access_grant_requests", limit=1000)
            if r.get("resource") == str(entity_id) and r.get("status") == "pending"
        ]
        return rows[offset : offset + limit]

    def get_entity_summary(self, *_, **__):
        return {}

    def get_user_summary(self, *_, **__):
        return {}

    def revoke_grant(self, db=None, grant_id=None, **_):
        return self.handle_request(db, grant_id, {"status": "revoked"})[0]

    def get_all_duration_configs(self, db=None, **_):
        return list(self._configs.values())

    def get_duration_config(self, db=None, entity_type=None, **_):
        return self._configs.get(entity_type)

    def get_duration_options(self, db=None, entity_type=None, **_):
        return (self._configs.get(entity_type) or {}).get("duration_options", [])

    def upsert_duration_config(self, db=None, config_in=None, **_):
        d = _data(config_in)
        self._configs[d.get("entity_type")] = d
        return d


class UcNativeWorkflowsManager:
    def __init__(self, workflows: UcNativeWorkflowStore) -> None:
        self._workflows = workflows

    def list_workflows(self, is_active=None, workflow_type=None, **_):
        out = []
        for row in self._workflows.list_workflow_definitions():
            doc = self._workflows._store.parse_snapshot(row) if hasattr(self._workflows._store, "parse_snapshot") else dict(row)
            if not isinstance(doc, dict):
                doc = {}
            doc.setdefault("id", row.get("id"))
            if is_active is not None and bool(doc.get("is_active", True)) != bool(is_active):
                continue
            if workflow_type is not None and doc.get("workflow_type") != workflow_type:
                continue
            out.append(doc)
        return out

    def get_workflow(self, workflow_id, **_):
        row = self._workflows._store.get_by_id("process_workflows", str(workflow_id))
        if not row:
            return None
        doc = self._workflows._store.parse_snapshot(row)
        doc["id"] = str(workflow_id)
        return doc

    def create_workflow(self, workflow, created_by=None, **_):
        doc = _data(workflow)
        doc.setdefault("id", str(uuid.uuid4()))
        doc.setdefault("status", "active")
        doc.setdefault("is_active", True)
        doc["created_by"] = created_by
        doc["updated_at"] = _now()
        self._workflows._store.merge_row(
            "process_workflows",
            {
                "id": doc["id"],
                "name": doc.get("name"),
                "entity_type": doc.get("entity_type"),
                "status": doc["status"],
                "updated_at": doc["updated_at"],
                "snapshot_json": json.dumps(doc, default=str),
            },
        )
        return doc

    def update_workflow(self, workflow_id, workflow, updated_by=None, **_):
        old = self.get_workflow(workflow_id)
        if not old:
            return None
        merged = {**old, **_data(workflow, exclude_unset=True), "id": str(workflow_id), "updated_by": updated_by}
        return self.create_workflow(merged, created_by=old.get("created_by"))

    def delete_workflow(self, workflow_id, **_):
        if not self.get_workflow(workflow_id):
            return False
        self._workflows._store.delete_by_id("process_workflows", str(workflow_id))
        return True

    def toggle_active(self, workflow_id, **_):
        current = self.get_workflow(workflow_id) or {}
        return self.update_workflow(workflow_id, {"is_active": not bool(current.get("is_active", True))})

    def duplicate_workflow(self, workflow_id, **_):
        doc = self.get_workflow(workflow_id)
        if not doc:
            return None
        return self.create_workflow(
            {**doc, "id": str(uuid.uuid4()), "name": f"{doc.get('name', 'Workflow')} copy"}
        )

    def validate_workflow(self, *_, **__):
        return {"issues": []}

    def get_step_type_schemas(self, *_, **__):
        return []

    def get_template_vars(self, trigger=None, entity_type=None, **_):
        return {}

    def get_workflow_by_trigger_type(self, trigger_type, entity_type=None, **_):
        for workflow in self.list_workflows():
            trigger = workflow.get("trigger") or {}
            if trigger.get("type") == trigger_type and (
                entity_type is None or workflow.get("entity_type") == entity_type
            ):
                return workflow
        return None

    def load_from_yaml(self, *_, **__):
        return {"created": 0, "updated": 0, "skipped": 0}
