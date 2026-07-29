"""SettingsManager backed by UC Delta (uc_native mode)."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from src.common.config import Settings
from src.common.features import ACCESS_LEVEL_ORDER, FeatureAccessLevel
from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore
from src.common.uc_native.rbac import UcNativeRbacStore
from src.db_models.settings import NO_ROLE_SENTINEL
from src.models.settings import AppRole

logger = get_logger(__name__)


class UcNativeSettingsManager:
    """Minimal SettingsManager surface for RBAC and app settings."""

    def __init__(self, store: DeltaStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self._rbac = UcNativeRbacStore(store, settings)
        self._role_overrides: Dict[str, str] = {}
        self._notifications_manager = None
        self._jobs = None

    def ensure_default_roles_exist(self) -> None:
        self._rbac.seed_default_roles()

    def list_app_roles(self) -> List[AppRole]:
        return self._rbac.list_roles()

    def list_app_roles_for_approval(self, approval_entity: Optional[str] = None) -> List[AppRole]:
        return self.list_app_roles()

    def get_feature_permissions_for_role_id(self, role_id: str) -> Dict[str, FeatureAccessLevel]:
        role = self._rbac.get_role_by_id(role_id)
        if not role:
            return {}
        return dict(role.feature_permissions)

    def get_app_role(self, role_id: str) -> Optional[AppRole]:
        return self._rbac.get_role_by_id(str(role_id))

    def get_app_role_by_name(self, role_name: str) -> Optional[AppRole]:
        return self._rbac.get_role_by_name(role_name)

    def _roles_for_groups(self, user_groups: List[str]) -> List[AppRole]:
        groups = set(user_groups)
        return [
            role
            for role in self.list_app_roles()
            if groups.intersection(set(role.assigned_groups or []))
        ]

    def get_canonical_role_for_groups(self, user_groups: Optional[List[str]]) -> Optional[AppRole]:
        """Map the caller's groups to their configured AppRole.

        Mirrors SettingsManager.get_canonical_role_for_groups: an "admin"-ish
        group name wins first (dev-friendly), otherwise the highest-privilege
        role whose assigned_groups the caller is in. The Postgres manager's
        distance-based fallback is omitted — uc_native roles come from the
        seeded roles YAML, so an unmatched caller genuinely has no role.
        """
        if not user_groups:
            return None

        roles = self.list_app_roles()
        if any("admin" in group.lower() for group in user_groups):
            admin = next(
                (r for r in roles if (r.name or "").strip().lower() == "admin"), None
            )
            if admin:
                return admin

        best_role: Optional[AppRole] = None
        best_weight = -1
        for role in self._roles_for_groups(list(user_groups)):
            weight = sum(
                ACCESS_LEVEL_ORDER.get(level, 0)
                for level in (role.feature_permissions or {}).values()
            )
            if weight > best_weight:
                best_weight = weight
                best_role = role
        return best_role

    def get_requestable_roles_for_user(
        self, user_groups: Optional[List[str]] = None
    ) -> List[AppRole]:
        """Roles the caller may request, per each role's requestable_by_roles.

        uc_native stores no role hierarchy today, so this is normally empty; it
        starts returning rows as soon as roles carry requestable_by_roles.
        """
        held_role_ids = {str(role.id) for role in self._roles_for_groups(list(user_groups or []))}
        requesters = held_role_ids or {NO_ROLE_SENTINEL}
        return [
            role
            for role in self.list_app_roles()
            if str(role.id) not in held_role_ids
            and requesters.intersection({str(r) for r in (role.requestable_by_roles or [])})
        ]

    def get_applied_role_override_for_user(self, user_email: str) -> Optional[str]:
        stored = self._store.get_setting(f"role_override:{user_email}")
        return stored or self._role_overrides.get(user_email)

    def set_applied_role_override_for_user(self, user_email: str, role_id: Optional[str]) -> None:
        if role_id:
            self._role_overrides[user_email] = role_id
            self._store.upsert_setting(f"role_override:{user_email}", role_id)
        else:
            self._role_overrides.pop(user_email, None)
            self._store.upsert_setting(f"role_override:{user_email}", "")

    def set_notifications_manager(self, manager) -> None:
        self._notifications_manager = manager

    def get_setting(self, key: str) -> Optional[str]:
        return self._store.get_setting(key)

    def set_setting(self, key: str, value: str) -> None:
        self._store.upsert_setting(key, value)

    def upgrade_admin_role_for_new_features(self) -> None:
        """No-op — Admin role is seeded with full permissions."""

    def ensure_default_team_and_project(self) -> None:
        """No-op in uc_native v1 — teams/projects stored in Delta when used."""
