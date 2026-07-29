"""Durable UC-native feature managers backed by Delta snapshots.

These adapters accept the existing route call shapes while ignoring the OLTP
session argument. Delta is the source of record in ``uc_native`` mode.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from src.common.uc_native.entities import UcNativeEntityStore
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.common.uc_native.workflows import UcNativeWorkflowStore
from src.common.errors import ConflictError, NotFoundError
from src.models.entity_subscriptions import (
    EntitySubscriptionRead,
    EntitySubscriptionSummary,
)
from src.models.metadata import (
    Document,
    Link,
    MergedMetadataResponse,
    MetadataAttachment,
    RichText,
    SharedAssetListResponse,
)
from src.models.projects import ProjectRead, ProjectSummary, UserProjectAccess
from src.models.quality import QualitySummary
from src.models.teams import TeamMemberRead, TeamRead, TeamSummary
from src.models.business_roles import BusinessRoleRead
from src.models.costs import CostSummary
from src.models.delivery_methods import DeliveryMethodRead


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


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

    def _to_member_read(self, team_id: str, member: Dict[str, Any]) -> TeamMemberRead:
        identifier = member.get("member_identifier") or ""
        return TeamMemberRead(
            id=str(member.get("id") or uuid.uuid4()),
            team_id=team_id,
            member_type=member.get("member_type") or "user",
            member_identifier=identifier,
            app_role_override=member.get("app_role_override"),
            member_name=member.get("member_name") or identifier,
            created_at=_parse_dt(member.get("created_at")),
            updated_at=_parse_dt(member.get("updated_at")),
            added_by=member.get("added_by") or "system",
        )

    def _to_team_read(self, doc: Dict[str, Any]) -> TeamRead:
        team_id = str(doc.get("id") or "")
        members = [
            self._to_member_read(team_id, member)
            for member in (doc.get("members") or [])
        ]
        created_by = doc.get("created_by") or "system"
        return TeamRead(
            id=team_id,
            name=doc.get("name") or "",
            title=doc.get("title"),
            description=doc.get("description"),
            domain_id=doc.get("domain_id"),
            domain_name=doc.get("domain_name"),
            tags=doc.get("tags") or [],
            metadata=doc.get("metadata"),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
            created_by=created_by,
            updated_by=doc.get("updated_by") or created_by,
            members=members,
        )

    def _to_team_summary(self, doc: Dict[str, Any]) -> TeamSummary:
        return TeamSummary(
            id=str(doc.get("id") or ""),
            name=doc.get("name") or "",
            title=doc.get("title"),
            domain_id=doc.get("domain_id"),
            member_count=len(doc.get("members") or []),
        )

    def create_team(self, db=None, team_in=None, current_user_id=None, **kwargs):
        payload = _data(team_in or kwargs)
        if current_user_id:
            payload["created_by"] = current_user_id
            payload["updated_by"] = current_user_id
        saved = self._save(payload)
        return self._to_team_read(saved)

    def get_all_teams(self, db=None, skip=0, limit=100, domain_id=None, **_):
        return [self._to_team_read(d) for d in self._list(skip, limit, domain_id=domain_id)]

    def get_teams_summary(self, db=None, domain_id=None, **_):
        return [
            self._to_team_summary(d)
            for d in self._list(0, 1000, domain_id=domain_id)
        ]

    def get_team_by_id(self, db=None, team_id=None, **_):
        doc = self._get(str(team_id))
        return self._to_team_read(doc) if doc else None

    def get_teams_by_domain(self, db=None, domain_id=None, **_):
        return [self._to_team_read(d) for d in self._list(0, 1000, domain_id=domain_id)]

    def get_standalone_teams(self, db=None, **_):
        return [
            self._to_team_read(d)
            for d in self._list(0, 1000)
            if not d.get("domain_id")
        ]

    def get_teams_for_user(self, db=None, user_identifier=None, user_groups=None, **_):
        groups = set(user_groups or [])
        matched = [
            d
            for d in self._list(0, 1000)
            if any(
                m.get("member_identifier") == user_identifier
                or m.get("member_identifier") in groups
                for m in d.get("members", [])
            )
        ]
        return [self._to_team_read(d) for d in matched]

    def update_team(self, db=None, team_id=None, team_in=None, current_user_id=None, **_):
        existing = self._get(str(team_id))
        if not existing:
            raise NotFoundError(f"Team with id '{team_id}' not found.")
        payload = _data(team_in, exclude_unset=True)
        if current_user_id:
            payload["updated_by"] = current_user_id
        saved = self._save(payload, existing=existing)
        return self._to_team_read(saved)

    def delete_team(self, db=None, team_id=None, **_):
        existing = self._get(str(team_id))
        if not existing:
            raise NotFoundError(f"Team with id '{team_id}' not found.")
        read_model = self._to_team_read(existing)
        self._delete(str(team_id))
        return read_model

    def add_team_member(self, db=None, team_id=None, member_in=None, current_user_id=None, **kwargs):
        team = self._get(str(team_id))
        if not team:
            raise NotFoundError(f"Team with id '{team_id}' not found.")
        member = _data(member_in or kwargs)
        now = _now()
        member.setdefault("id", str(uuid.uuid4()))
        member.setdefault("added_by", current_user_id or "system")
        member.setdefault("created_at", now)
        member.setdefault("updated_at", now)
        team.setdefault("members", []).append(member)
        self._save(team, existing=team)
        return self._to_member_read(str(team_id), member)

    def get_team_members(self, db=None, team_id=None, **_):
        team = self._get(str(team_id)) or {}
        return [
            self._to_member_read(str(team_id), member)
            for member in team.get("members", [])
        ]

    def update_team_member(self, db=None, team_id=None, member_id=None, member_in=None, **_):
        team = self._get(str(team_id))
        if not team:
            raise NotFoundError(f"Team with id '{team_id}' not found.")
        for member in team.get("members", []):
            if str(member.get("id")) == str(member_id):
                member.update(_data(member_in, exclude_unset=True))
                member["updated_at"] = _now()
                self._save(team, existing=team)
                return self._to_member_read(str(team_id), member)
        raise NotFoundError(f"Team member with id '{member_id}' not found.")

    def remove_team_member(self, db=None, team_id=None, member_identifier=None, **_):
        team = self._get(str(team_id))
        if not team:
            raise NotFoundError(f"Team with id '{team_id}' not found.")
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
        raise NotFoundError(f"Team member '{member_identifier}' not found.")


class UcNativeProjectsManager(_EntityCrud):
    table_name = "projects"

    def _to_project_read(self, doc: Dict[str, Any]) -> ProjectRead:
        created_by = doc.get("created_by") or "system"
        team_ids = doc.get("team_ids") or []
        teams = [
            TeamSummary(
                id=str(team_id),
                name=str(team_id),
                title=None,
                domain_id=None,
                member_count=0,
            )
            for team_id in team_ids
        ]
        return ProjectRead(
            id=str(doc.get("id") or ""),
            name=doc.get("name") or "",
            title=doc.get("title"),
            description=doc.get("description"),
            owner_team_id=doc.get("owner_team_id"),
            owner_team_name=doc.get("owner_team_name"),
            project_type=doc.get("project_type") or "TEAM",
            tags=doc.get("tags") or [],
            metadata=doc.get("metadata"),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
            created_by=created_by,
            updated_by=doc.get("updated_by") or created_by,
            teams=teams,
        )

    def _to_project_summary(self, doc: Dict[str, Any]) -> ProjectSummary:
        return ProjectSummary(
            id=str(doc.get("id") or ""),
            name=doc.get("name") or "",
            title=doc.get("title"),
            team_count=len(doc.get("team_ids") or []),
        )

    def create_project(self, db=None, project_in=None, current_user_id=None, **kwargs):
        payload = _data(project_in or kwargs)
        if current_user_id:
            payload["created_by"] = current_user_id
            payload["updated_by"] = current_user_id
        saved = self._save(payload)
        return self._to_project_read(saved)

    def get_all_projects(self, db=None, skip=0, limit=100, **_):
        return [self._to_project_read(d) for d in self._list(skip, limit)]

    def get_projects_summary(self, db=None, **_):
        return [self._to_project_summary(d) for d in self._list(0, 1000)]

    def get_project_by_id(self, db=None, project_id=None, **_):
        doc = self._get(str(project_id))
        return self._to_project_read(doc) if doc else None

    def update_project(self, db=None, project_id=None, project_in=None, current_user_id=None, **_):
        old = self._get(str(project_id))
        if not old:
            raise NotFoundError(f"Project with id '{project_id}' not found.")
        payload = _data(project_in, exclude_unset=True)
        if current_user_id:
            payload["updated_by"] = current_user_id
        saved = self._save(payload, existing=old)
        return self._to_project_read(saved)

    def delete_project(self, db=None, project_id=None, **_):
        existing = self._get(str(project_id))
        if not existing:
            raise NotFoundError(f"Project with id '{project_id}' not found.")
        read_model = self._to_project_read(existing)
        self._delete(str(project_id))
        return read_model

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

    def _accessible_project_docs(
        self,
        *,
        user_identifier: Optional[str] = None,
        user_groups: Optional[List[str]] = None,
        team_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Projects the caller reaches through team membership.

        Mirrors ProjectsManager.get_user_projects: admins see every project,
        everyone else sees the projects whose assigned teams they belong to.
        """
        docs = self._list(0, 1000)
        groups = [str(g) for g in (user_groups or [])]
        if any("admin" in group.lower() for group in groups):
            return docs

        member_team_ids = {str(t) for t in (team_ids or [])}
        if not member_team_ids and user_identifier:
            teams = UcNativeTeamsManager(self._entities).get_teams_for_user(
                user_identifier=user_identifier,
                user_groups=groups,
            )
            member_team_ids = {str(team.id) for team in teams}
        # Older rows stored group names straight into team_ids.
        member_team_ids |= set(groups)

        uid = str(user_identifier or "")
        matched = []
        for doc in docs:
            members = [str(m) for m in (doc.get("member_ids") or [])]
            doc_team_ids = {str(t) for t in (doc.get("team_ids") or [])}
            if (uid and uid in members) or (doc_team_ids & member_team_ids):
                matched.append(doc)
        return matched

    def get_user_projects(self, db=None, user_id=None, user_identifier=None, team_ids=None, user_groups=None, **_):
        docs = self._accessible_project_docs(
            user_identifier=user_id or user_identifier,
            user_groups=user_groups,
            team_ids=team_ids,
        )
        return UserProjectAccess(
            projects=[self._to_project_summary(doc) for doc in docs],
            current_project_id=None,
        )

    def check_user_project_access(
        self, db=None, user_identifier=None, user_groups=None, project_id=None, user_id=None, team_ids=None, **_
    ):
        # Positional call shape mirrors ProjectsManager.check_user_project_access
        # (db, user_identifier, user_groups, project_id); the user_id/team_ids
        # aliases keep older keyword callers working.
        docs = self._accessible_project_docs(
            user_identifier=user_id or user_identifier,
            user_groups=user_groups,
            team_ids=team_ids,
        )
        return any(str(doc.get("id")) == str(project_id) for doc in docs)

    def is_user_project_member(
        self, db=None, user_identifier=None, user_groups=None, project_id=None, settings=None, **_
    ):
        # Positional shape mirrors ProjectsManager.is_user_project_member
        # (db, user_identifier, user_groups, project_id, settings).
        if settings is not None:
            from src.common.authorization import is_user_admin

            if is_user_admin(list(user_groups or []), settings):
                return True
        return self.check_user_project_access(
            db,
            user_identifier=user_identifier,
            user_groups=user_groups,
            project_id=project_id,
        )

    def request_project_access(self, *_, **__):
        return None


class UcNativeBusinessRolesManager(_EntityCrud):
    table_name = "business_roles"

    def _to_role_read(self, doc: Dict[str, Any]) -> BusinessRoleRead:
        role_id = doc.get("id") or str(uuid.uuid4())
        return BusinessRoleRead(
            id=UUID(str(role_id)),
            name=doc.get("name") or "",
            description=doc.get("description"),
            category=doc.get("category"),
            is_system=bool(doc.get("is_system", False)),
            status=doc.get("status") or "active",
            is_approver=bool(doc.get("is_approver", False)),
            created_by=doc.get("created_by"),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
        )

    def create_role(self, db=None, role_in=None, current_user_id=None, **kwargs):
        payload = _data(role_in or kwargs)
        if current_user_id:
            payload["created_by"] = current_user_id
        # Persist enum values as plain strings for Delta snapshots.
        if hasattr(payload.get("category"), "value"):
            payload["category"] = payload["category"].value
        if hasattr(payload.get("status"), "value"):
            payload["status"] = payload["status"].value
        saved = self._save(payload)
        return self._to_role_read(saved)

    def get_role(self, db=None, role_id=None, **_):
        doc = self._get(str(role_id))
        return self._to_role_read(doc) if doc else None

    def get_all_roles(self, db=None, skip=0, limit=100, category=None, status=None, **_):
        category_value = category.value if hasattr(category, "value") else category
        status_value = status.value if hasattr(status, "value") else status
        return [
            self._to_role_read(doc)
            for doc in self._list(skip, limit, category=category_value, status=status_value)
        ]

    def update_role(self, db=None, role_id=None, role_in=None, current_user_id=None, **_):
        old = self._get(str(role_id))
        if not old:
            raise NotFoundError(f"Business role with id '{role_id}' not found.")
        payload = _data(role_in, exclude_unset=True)
        if hasattr(payload.get("category"), "value"):
            payload["category"] = payload["category"].value
        if hasattr(payload.get("status"), "value"):
            payload["status"] = payload["status"].value
        saved = self._save(payload, existing=old)
        return self._to_role_read(saved)

    def delete_role(self, db=None, role_id=None, **_):
        old = self._get(str(role_id))
        if not old:
            raise NotFoundError(f"Business role with id '{role_id}' not found.")
        read_model = self._to_role_read(old)
        self._delete(str(role_id))
        return read_model


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
        doc = _data(obj_in or kwargs)
        if current_user_id:
            doc.setdefault("created_by", current_user_id)
        # Routes read ``.id`` off the result, so return the read model rather than the raw doc.
        return DeliveryMethodRead.model_validate(self._save(doc))

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

    @staticmethod
    def _month_start(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return date(value.year, value.month, 1)
        if isinstance(value, date):
            return date(value.year, value.month, 1)
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return date(parsed.year, parsed.month, 1)

    @classmethod
    def _active_in_month(cls, row: Dict[str, Any], month_start: date) -> bool:
        """Same window as CostItemsRepository: started by, not ended before."""
        start = cls._month_start(row.get("start_month"))
        end = cls._month_start(row.get("end_month"))
        if start is None or start > month_start:
            return False
        return end is None or end >= month_start

    def list(self, db=None, entity_type=None, entity_id=None, month=None, **_):
        rows = super().list(db, entity_type=entity_type, entity_id=entity_id)
        month_start = self._month_start(month)
        if month_start is not None:
            rows = [row for row in rows if self._active_in_month(row, month_start)]
        rows.sort(key=lambda row: str(row.get("start_month") or ""))
        return rows

    def summarize(self, db=None, entity_type=None, entity_id=None, month=None, **_) -> CostSummary:
        month_start = self._month_start(month) or date.today().replace(day=1)
        rows = self.list(db, entity_type=entity_type, entity_id=entity_id, month=month_start)
        by_center: Dict[str, int] = {}
        for row in rows:
            center = str(row.get("cost_center") or "OTHER")
            try:
                amount = int(row.get("amount_cents") or 0)
            except (TypeError, ValueError):
                amount = 0
            by_center[center] = by_center.get(center, 0) + amount
        return CostSummary(
            month=f"{month_start.year:04d}-{month_start.month:02d}",
            currency=str(rows[0].get("currency") or "USD") if rows else "USD",
            total_cents=sum(by_center.values()),
            items_count=len(rows),
            by_center=by_center,
        )


class UcNativeQualityManager(_OverlayItems):
    table_name = "quality_items"

    @staticmethod
    def _score(row: Dict[str, Any]) -> float:
        try:
            return float(row.get("score_percent") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _measured(row: Dict[str, Any]) -> datetime:
        """Sort key for "latest measurement"; unmeasured rows lose every tie."""
        value = row.get("measured_at")
        return _parse_dt(value) if value else datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def _summarize_rows(cls, rows: List[Dict[str, Any]]) -> QualitySummary:
        """Average the latest measurement per (entity, dimension).

        Mirrors QualityItemsRepository.summarize_for_entity and
        QualityManager.aggregate_for_product: dedupe to the newest row per
        dimension (per entity, when rolling several entities up), then average
        those into the per-dimension, per-source and overall scores.
        """
        latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for row in rows:
            dimension = str(row.get("dimension") or "")
            if not dimension:
                continue
            key = (
                str(row.get("entity_type") or ""),
                str(row.get("entity_id") or ""),
                dimension,
            )
            current = latest.get(key)
            if current is None or cls._measured(row) > cls._measured(current):
                latest[key] = row

        if not latest:
            return QualitySummary(
                overall_score_percent=0.0,
                items_count=0,
                by_dimension={},
                by_source={},
                measured_at=None,
            )

        by_dimension: Dict[str, float] = {}
        dim_counts: Dict[str, int] = {}
        by_source: Dict[str, float] = {}
        source_counts: Dict[str, int] = {}
        measured_at: Optional[datetime] = None

        for row in latest.values():
            dimension = str(row.get("dimension"))
            source = str(row.get("source") or "manual")
            score = cls._score(row)
            by_dimension[dimension] = by_dimension.get(dimension, 0.0) + score
            dim_counts[dimension] = dim_counts.get(dimension, 0) + 1
            by_source[source] = by_source.get(source, 0.0) + score
            source_counts[source] = source_counts.get(source, 0) + 1
            if row.get("measured_at"):
                ts = cls._measured(row)
                if measured_at is None or ts > measured_at:
                    measured_at = ts

        for dimension in by_dimension:
            by_dimension[dimension] = round(by_dimension[dimension] / dim_counts[dimension], 2)
        for source in by_source:
            by_source[source] = round(by_source[source] / source_counts[source], 2)

        return QualitySummary(
            overall_score_percent=round(sum(by_dimension.values()) / len(by_dimension), 2),
            items_count=len(rows),
            by_dimension=by_dimension,
            by_source=by_source,
            measured_at=measured_at,
        )

    def summarize(self, db=None, entity_type=None, entity_id=None, **_) -> QualitySummary:
        return self._summarize_rows(
            self.list(db, entity_type=entity_type, entity_id=entity_id)
        )

    def aggregate_for_product(
        self, db=None, product_id=None, data_products_manager=None, **_
    ) -> QualitySummary:
        """Roll the product's own measurements up with its contracts'."""
        rows = self.list(db, entity_type="data_product", entity_id=str(product_id))
        contract_ids: List[str] = []
        if data_products_manager is not None:
            try:
                contract_ids = list(
                    data_products_manager.get_contracts_for_product(str(product_id)) or []
                )
            except Exception:  # pragma: no cover - defensive, summary is non-critical
                contract_ids = []
        for contract_id in contract_ids:
            rows.extend(self.list(db, entity_type="data_contract", entity_id=str(contract_id)))
        return self._summarize_rows(rows)


SHARED_ENTITY_ID = "__shared__"


class UcNativeMetadataManager(_OverlayItems):
    """Rich texts, links, documents and attachments for the entity metadata panels.

    All four kinds share the ``metadata_items`` overlay table and are told apart by
    a ``kind`` discriminator, so uc_native needs no per-kind Delta table.
    """

    table_name = "metadata_items"

    # --- shared plumbing ---

    def _save_asset(
        self,
        kind: str,
        payload: Any,
        *,
        existing: Optional[Dict[str, Any]] = None,
        user_email: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        doc = dict(existing or {})
        doc.update(_data(payload, exclude_unset=existing is not None))
        doc.update(extra or {})
        doc["kind"] = kind
        doc.setdefault("id", str(uuid.uuid4()))
        doc.setdefault("created_at", _now())
        doc.setdefault("created_by", user_email)
        doc["updated_at"] = _now()
        doc["updated_by"] = user_email
        return self._overlays.add(self.table_name, doc)

    def _items(self, kind: str, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        rows = self._overlays.list_for_entity(self.table_name, entity_type, str(entity_id))
        items = [row for row in rows if row.get("kind") == kind]
        items.sort(key=lambda row: (int(row.get("level") or 50), str(row.get("created_at") or "")))
        return items

    def _item(self, kind: str, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._overlays.get(self.table_name, str(item_id))
        return row if row and row.get("kind") == kind else None

    def _remove(self, kind: str, item_id: str) -> bool:
        if not self._item(kind, item_id):
            return False
        return self._overlays.remove(self.table_name, str(item_id))

    def _all(self, kind: str) -> List[Dict[str, Any]]:
        return [
            row
            for row in self._overlays.list_all(self.table_name)
            if row.get("kind") == kind
        ]

    @staticmethod
    def _rich_text(doc: Dict[str, Any]) -> RichText:
        return RichText(
            id=UUID(str(doc["id"])),
            entity_id=str(doc.get("entity_id") or ""),
            entity_type=str(doc.get("entity_type") or ""),
            title=str(doc.get("title") or ""),
            short_description=doc.get("short_description"),
            content_markdown=str(doc.get("content_markdown") or ""),
            is_shared=bool(doc.get("is_shared", False)),
            level=int(doc.get("level") or 50),
            inheritable=bool(doc.get("inheritable", True)),
            created_by=doc.get("created_by"),
            updated_by=doc.get("updated_by"),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
        )

    @staticmethod
    def _link(doc: Dict[str, Any]) -> Link:
        return Link(
            id=UUID(str(doc["id"])),
            entity_id=str(doc.get("entity_id") or ""),
            entity_type=str(doc.get("entity_type") or ""),
            title=str(doc.get("title") or ""),
            short_description=doc.get("short_description"),
            url=str(doc.get("url") or ""),
            is_shared=bool(doc.get("is_shared", False)),
            level=int(doc.get("level") or 50),
            inheritable=bool(doc.get("inheritable", True)),
            created_by=doc.get("created_by"),
            updated_by=doc.get("updated_by"),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
        )

    @staticmethod
    def _document(doc: Dict[str, Any]) -> Document:
        size = doc.get("size_bytes")
        return Document(
            id=UUID(str(doc["id"])),
            entity_id=str(doc.get("entity_id") or ""),
            entity_type=str(doc.get("entity_type") or ""),
            title=str(doc.get("title") or ""),
            short_description=doc.get("short_description"),
            is_shared=bool(doc.get("is_shared", False)),
            level=int(doc.get("level") or 50),
            inheritable=bool(doc.get("inheritable", True)),
            original_filename=str(doc.get("original_filename") or ""),
            content_type=doc.get("content_type"),
            size_bytes=int(size) if size not in (None, "") else None,
            storage_path=str(doc.get("storage_path") or ""),
            created_by=doc.get("created_by"),
            updated_by=doc.get("updated_by"),
            created_at=_parse_dt(doc.get("created_at")),
            updated_at=_parse_dt(doc.get("updated_at")),
        )

    @staticmethod
    def _attachment(doc: Dict[str, Any]) -> MetadataAttachment:
        override = doc.get("level_override")
        return MetadataAttachment(
            id=UUID(str(doc["id"])),
            entity_id=str(doc.get("entity_id") or ""),
            entity_type=str(doc.get("entity_type") or ""),
            asset_type=str(doc.get("asset_type") or ""),
            asset_id=str(doc.get("asset_id") or ""),
            level_override=int(override) if override not in (None, "") else None,
            created_by=doc.get("created_by"),
            created_at=_parse_dt(doc.get("created_at")),
        )

    # --- volume management (documents) ---

    def ensure_volume_path(self, ws, settings, base_dir: str) -> str:
        from src.controller.metadata_manager import ensure_app_volume_path

        return ensure_app_volume_path(ws, settings)

    # --- rich text ---

    def create_rich_text(self, db=None, *, data=None, user_email=None, **_) -> RichText:
        return self._rich_text(self._save_asset("rich_text", data, user_email=user_email))

    def list_rich_texts(self, db=None, *, entity_type=None, entity_id=None, **_) -> List[RichText]:
        return [self._rich_text(row) for row in self._items("rich_text", entity_type, entity_id)]

    def update_rich_text(self, db=None, *, id=None, data=None, user_email=None, **_) -> Optional[RichText]:
        existing = self._item("rich_text", id)
        if not existing:
            return None
        return self._rich_text(
            self._save_asset("rich_text", data, existing=existing, user_email=user_email)
        )

    def delete_rich_text(self, db=None, *, id=None, user_email=None, **_) -> bool:
        return self._remove("rich_text", id)

    # --- links ---

    def create_link(self, db=None, *, data=None, user_email=None, **_) -> Link:
        return self._link(self._save_asset("link", data, user_email=user_email))

    def list_links(self, db=None, *, entity_type=None, entity_id=None, **_) -> List[Link]:
        return [self._link(row) for row in self._items("link", entity_type, entity_id)]

    def update_link(self, db=None, *, id=None, data=None, user_email=None, **_) -> Optional[Link]:
        existing = self._item("link", id)
        if not existing:
            return None
        return self._link(self._save_asset("link", data, existing=existing, user_email=user_email))

    def delete_link(self, db=None, *, id=None, user_email=None, **_) -> bool:
        return self._remove("link", id)

    # --- documents ---

    def create_document_record(
        self,
        db=None,
        *,
        data=None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        storage_path: Optional[str] = None,
        user_email: Optional[str] = None,
        **_,
    ) -> Document:
        return self._document(
            self._save_asset(
                "document",
                data,
                user_email=user_email,
                extra={
                    "original_filename": filename,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                    "storage_path": storage_path,
                },
            )
        )

    def list_documents(self, db=None, *, entity_type=None, entity_id=None, **_) -> List[Document]:
        return [self._document(row) for row in self._items("document", entity_type, entity_id)]

    def get_document(self, db=None, *, id=None, **_) -> Optional[Document]:
        existing = self._item("document", id)
        return self._document(existing) if existing else None

    def delete_document(self, db=None, *, id=None, user_email=None, **_) -> bool:
        return self._remove("document", id)

    # --- shared assets ---

    def list_shared_assets(self, db=None, *, entity_type=None, **_) -> SharedAssetListResponse:
        def shared(kind: str) -> List[Dict[str, Any]]:
            return [
                row
                for row in self._all(kind)
                if row.get("is_shared")
                and (entity_type is None or row.get("entity_type") == entity_type)
            ]

        return SharedAssetListResponse(
            rich_texts=[self._rich_text(row) for row in shared("rich_text")],
            links=[self._link(row) for row in shared("link")],
            documents=[self._document(row) for row in shared("document")],
        )

    def create_shared_rich_text(self, db=None, *, data=None, user_email=None, **_) -> RichText:
        return self._rich_text(
            self._save_asset(
                "rich_text",
                data,
                user_email=user_email,
                extra={"entity_id": SHARED_ENTITY_ID, "is_shared": True},
            )
        )

    def create_shared_link(self, db=None, *, data=None, user_email=None, **_) -> Link:
        return self._link(
            self._save_asset(
                "link",
                data,
                user_email=user_email,
                extra={"entity_id": SHARED_ENTITY_ID, "is_shared": True},
            )
        )

    # --- attachments ---

    def attach_shared_asset(
        self,
        db=None,
        *,
        entity_type=None,
        entity_id=None,
        data=None,
        user_email=None,
        **_,
    ) -> MetadataAttachment:
        payload = _data(data)
        existing = next(
            (
                row
                for row in self._items("attachment", entity_type, entity_id)
                if row.get("asset_type") == payload.get("asset_type")
                and str(row.get("asset_id")) == str(payload.get("asset_id"))
            ),
            None,
        )
        if existing:
            if payload.get("level_override") is None:
                return self._attachment(existing)
            existing["level_override"] = payload["level_override"]
            return self._attachment(
                self._save_asset("attachment", None, existing=existing, user_email=user_email)
            )
        return self._attachment(
            self._save_asset(
                "attachment",
                data,
                user_email=user_email,
                extra={"entity_type": entity_type, "entity_id": str(entity_id)},
            )
        )

    def list_attachments(
        self, db=None, *, entity_type=None, entity_id=None, **_
    ) -> List[MetadataAttachment]:
        return [
            self._attachment(row) for row in self._items("attachment", entity_type, entity_id)
        ]

    def detach_shared_asset(
        self,
        db=None,
        *,
        entity_type=None,
        entity_id=None,
        asset_type=None,
        asset_id=None,
        user_email=None,
        **_,
    ) -> bool:
        for row in self._items("attachment", entity_type, entity_id):
            if row.get("asset_type") == asset_type and str(row.get("asset_id")) == str(asset_id):
                return self._overlays.remove(self.table_name, str(row["id"]))
        return False

    # --- merged view ---

    def get_merged_metadata(
        self,
        db=None,
        *,
        entity_type=None,
        entity_id=None,
        contract_ids: Optional[List[str]] = None,
        max_level_inheritance: int = 99,
        **_,
    ) -> MergedMetadataResponse:
        sources: Dict[str, str] = {}
        buckets: Dict[str, List[Dict[str, Any]]] = {"rich_text": [], "link": [], "document": []}

        for kind in buckets:
            for row in self._items(kind, entity_type, entity_id):
                sources[str(row["id"])] = str(entity_id)
                buckets[kind].append(row)

        # Shared assets attached to this entity.
        attachments = self._items("attachment", entity_type, entity_id)
        if attachments:
            by_kind = {kind: {str(row["id"]): row for row in self._all(kind)} for kind in buckets}
            for attachment in attachments:
                kind = str(attachment.get("asset_type") or "")
                asset = by_kind.get(kind, {}).get(str(attachment.get("asset_id")))
                if not asset:
                    continue
                sources[str(asset["id"])] = f"shared:{entity_id}"
                buckets[kind].append(asset)

        # Inheritable metadata from associated contracts.
        for contract_id in contract_ids or []:
            for kind in buckets:
                for row in self._items(kind, "data_contract", contract_id):
                    if not row.get("inheritable", True):
                        continue
                    if int(row.get("level") or 50) > max_level_inheritance:
                        continue
                    sources[str(row["id"])] = f"contract:{contract_id}"
                    buckets[kind].append(row)

        def dedupe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            rows = sorted(
                rows,
                key=lambda row: (int(row.get("level") or 50), str(row.get("created_at") or "")),
            )
            seen: set[str] = set()
            unique = []
            for row in rows:
                key = str(row["id"])
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
            return unique

        return MergedMetadataResponse(
            rich_texts=[self._rich_text(row) for row in dedupe(buckets["rich_text"])],
            links=[self._link(row) for row in dedupe(buckets["link"])],
            documents=[self._document(row) for row in dedupe(buckets["document"])],
            sources=sources,
        )


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
