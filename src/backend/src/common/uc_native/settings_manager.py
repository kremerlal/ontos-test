"""SettingsManager backed by UC Delta (uc_native mode)."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from src.common.config import Settings
from src.common.features import FeatureAccessLevel
from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore
from src.common.uc_native.rbac import UcNativeRbacStore
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
