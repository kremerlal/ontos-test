"""RBAC stored in UC Delta for uc_native mode."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.common.config import Settings, parse_group_list
from src.common.features import APP_FEATURES, FeatureAccessLevel
from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore
from src.models.settings import AppRole, HomeSection

logger = get_logger(__name__)

ROLES_YAML = Path(__file__).resolve().parents[2] / "data" / "settings.yaml"


def _parse_feature_level(raw: Any) -> FeatureAccessLevel:
    if isinstance(raw, FeatureAccessLevel):
        return raw
    if raw is None:
        return FeatureAccessLevel.NONE
    text = str(raw).strip().lower().replace("_", " ").replace("-", " ")
    mapping = {
        "none": FeatureAccessLevel.NONE,
        "read only": FeatureAccessLevel.READ_ONLY,
        "readonly": FeatureAccessLevel.READ_ONLY,
        "read write": FeatureAccessLevel.READ_WRITE,
        "readwrite": FeatureAccessLevel.READ_WRITE,
        "read/write": FeatureAccessLevel.READ_WRITE,
        "admin": FeatureAccessLevel.ADMIN,
        "full": FeatureAccessLevel.ADMIN,
    }
    return mapping.get(text, FeatureAccessLevel.NONE)


def _admin_permissions() -> Dict[str, FeatureAccessLevel]:
    return {feature_id: FeatureAccessLevel.ADMIN for feature_id in APP_FEATURES}


def _role_row_to_model(row: Dict[str, Any]) -> AppRole:
    groups = json.loads(row.get("assigned_groups_json") or "[]")
    perms_raw = json.loads(row.get("feature_permissions_json") or "{}")
    perms = {k: _parse_feature_level(v) for k, v in perms_raw.items()}
    sections_raw = json.loads(row.get("home_sections_json") or "[]")
    sections: List[HomeSection] = []
    for section in sections_raw:
        try:
            sections.append(HomeSection(section))
        except ValueError:
            continue
    return AppRole(
        id=row["id"],
        name=row["name"],
        description=row.get("description"),
        assigned_groups=groups,
        feature_permissions=perms,
        home_sections=sections,
        is_admin_role=bool(row.get("is_admin_role")),
    )


class UcNativeRbacStore:
    def __init__(self, store: DeltaStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    def list_roles(self) -> List[AppRole]:
        rows = self._store.list_rows("app_roles", limit=100)
        return [_role_row_to_model(row) for row in rows]

    def get_role_by_id(self, role_id: str) -> Optional[AppRole]:
        row = self._store.get_by_id("app_roles", role_id)
        return _role_row_to_model(row) if row else None

    def get_role_by_name(self, name: str) -> Optional[AppRole]:
        rows = self._store.query(
            f"SELECT * FROM {self._store.table_fqn('app_roles')} "
            f"WHERE name = '{name.replace(chr(39), chr(39)+chr(39))}' LIMIT 1"
        )
        return _role_row_to_model(rows[0]) if rows else None

    def seed_default_roles(self) -> int:
        existing = self._store.list_rows("app_roles", limit=200)
        if existing:
            self._reconcile_admin_groups(existing)
            logger.info("UC native roles already seeded (%s rows)", len(existing))
            return 0

        if not ROLES_YAML.exists():
            logger.warning("Roles YAML not found at %s", ROLES_YAML)
            return 0

        with open(ROLES_YAML, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        roles_data = data.get("roles") or []
        admin_groups = self._admin_groups()
        count = 0
        for role_def in roles_data:
            name = role_def.get("name")
            if not name:
                continue
            is_admin = name == "Admin"
            groups = list(role_def.get("assigned_groups") or [])
            if is_admin:
                # Always merge APP_ADMIN_DEFAULT_GROUPS so Admin stays reachable.
                groups = list(dict.fromkeys([*groups, *admin_groups]))
            perms: Dict[str, FeatureAccessLevel]
            if is_admin:
                perms = _admin_permissions()
            else:
                perms = {
                    k: _parse_feature_level(v)
                    for k, v in (role_def.get("feature_permissions") or {}).items()
                }
            sections = role_def.get("home_sections") or []
            self._store.merge_row(
                "app_roles",
                {
                    "id": str(uuid.uuid4()),
                    "name": name,
                    "description": role_def.get("description"),
                    "assigned_groups_json": json.dumps(groups),
                    "feature_permissions_json": json.dumps(
                        {k: v.value for k, v in perms.items()}
                    ),
                    "home_sections_json": json.dumps(sections),
                    "is_admin_role": is_admin,
                },
            )
            count += 1
        logger.info("Seeded %s UC native roles", count)
        return count

    def _reconcile_admin_groups(self, roles: List[Dict[str, Any]]) -> None:
        """Ensure existing Admin roles include every configured admin group."""
        configured = self._admin_groups()
        for role in roles:
            name = str(role.get("name") or "")
            is_admin_flag = role.get("is_admin_role")
            is_admin = name == "Admin" or str(is_admin_flag).lower() in ("true", "1", "yes")
            if not is_admin:
                continue
            try:
                existing = json.loads(role.get("assigned_groups_json") or "[]")
                if not isinstance(existing, list):
                    existing = []
            except json.JSONDecodeError:
                existing = []
            groups = list(dict.fromkeys([*[str(g) for g in existing], *configured]))
            if groups == existing:
                continue
            updated = dict(role)
            updated["assigned_groups_json"] = json.dumps(groups)
            # Keep Admin permissions full in case an earlier seed wrote {}.
            perms_raw = role.get("feature_permissions_json") or "{}"
            try:
                perms_obj = json.loads(perms_raw)
            except json.JSONDecodeError:
                perms_obj = {}
            if not perms_obj:
                updated["feature_permissions_json"] = json.dumps(
                    {k: v.value for k, v in _admin_permissions().items()}
                )
            self._store.merge_row("app_roles", updated)
            logger.info("Updated UC native Admin groups: %s", groups)

    def _admin_groups(self) -> List[str]:
        return parse_group_list(self._settings.APP_ADMIN_DEFAULT_GROUPS) or ["admins", "users"]
