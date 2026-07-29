"""Phase 1 UC-native org entity manager tests (domains, teams, projects)."""

from uuid import uuid4

import pytest

from src.common.errors import NotFoundError
from src.common.uc_native.feature_managers import (
    UcNativeProjectsManager,
    UcNativeTeamsManager,
)
from src.common.uc_native.managers import UcNativeDataDomainManager
from src.models.data_domains import DataDomainCreate, DataDomainUpdate
from src.models.projects import ProjectCreate
from src.models.teams import TeamCreate, TeamMemberCreate, TeamMemberUpdate


class FakeEntities:
    def __init__(self):
        self.tables = {}

    def save_entity(self, table, payload, **_):
        doc = dict(payload)
        self.tables.setdefault(table, {})[str(doc["id"])] = doc
        return dict(doc)

    def list_entities(self, table, limit=500):
        return list(self.tables.get(table, {}).values())[:limit]

    def get_entity(self, table, item_id):
        return self.tables.get(table, {}).get(str(item_id))

    def delete_entity(self, table, item_id):
        self.tables.get(table, {}).pop(str(item_id), None)


def test_domain_crud_round_trip():
    manager = UcNativeDataDomainManager(FakeEntities())
    created = manager.create_domain(
        domain_in=DataDomainCreate(name="Hotel Sales", description="Sales domain"),
        current_user_id="tester@example.com",
    )
    assert created.name == "Hotel Sales"
    assert created.created_by == "tester@example.com"

    listed = manager.get_all_domains()
    assert [d.id for d in listed] == [created.id]

    fetched = manager.get_domain_by_id(None, created.id)
    assert fetched is not None
    assert fetched.name == "Hotel Sales"

    updated = manager.update_domain(
        None,
        created.id,
        DataDomainUpdate(description="Updated"),
        "tester@example.com",
    )
    assert updated.description == "Updated"

    deleted = manager.delete_domain(None, created.id, current_user_id="tester@example.com")
    assert deleted.id == created.id
    assert manager.get_all_domains() == []


def test_domain_delete_missing_raises():
    manager = UcNativeDataDomainManager(FakeEntities())
    with pytest.raises(NotFoundError):
        manager.delete_domain(None, uuid4())


def test_team_with_member_and_delete():
    manager = UcNativeTeamsManager(FakeEntities())
    team = manager.create_team(
        team_in=TeamCreate(name="Hotel Sales Ops", title="Ops"),
        current_user_id="tester@example.com",
    )
    member = manager.add_team_member(
        team_id=team.id,
        member_in=TeamMemberCreate(member_type="user", member_identifier="alice@example.com"),
        current_user_id="tester@example.com",
    )
    assert member.member_identifier == "alice@example.com"
    assert member.team_id == team.id

    members = manager.get_team_members(team_id=team.id)
    assert len(members) == 1

    updated_member = manager.update_team_member(
        team_id=team.id,
        member_id=member.id,
        member_in=TeamMemberUpdate(app_role_override="Admin"),
    )
    assert updated_member.app_role_override == "Admin"

    user_teams = manager.get_teams_for_user(user_identifier="alice@example.com")
    assert [t.id for t in user_teams] == [team.id]

    deleted = manager.delete_team(team_id=team.id)
    assert deleted.id == team.id
    assert manager.get_all_teams() == []


def test_project_create_list_delete():
    manager = UcNativeProjectsManager(FakeEntities())
    project = manager.create_project(
        project_in=ProjectCreate(name="Hotel RevPar", title="RevPAR initiative"),
        current_user_id="tester@example.com",
    )
    assert project.name == "Hotel RevPar"
    assert manager.get_all_projects()[0].id == project.id

    deleted = manager.delete_project(project_id=project.id)
    assert deleted.id == project.id
    with pytest.raises(NotFoundError):
        manager.delete_project(project_id=project.id)


def test_business_role_crud_round_trip():
    from src.common.uc_native.feature_managers import UcNativeBusinessRolesManager
    from src.models.business_roles import BusinessRoleCreate, BusinessRoleUpdate

    manager = UcNativeBusinessRolesManager(FakeEntities())
    created = manager.create_role(
        role_in=BusinessRoleCreate(name="Data Owner", description="Owns the data", category="governance"),
        current_user_id="tester@example.com",
    )
    assert created.name == "Data Owner"
    assert created.created_by == "tester@example.com"

    listed = manager.get_all_roles()
    assert [r.id for r in listed] == [created.id]

    updated = manager.update_role(
        role_id=created.id,
        role_in=BusinessRoleUpdate(description="Updated"),
    )
    assert updated.description == "Updated"

    deleted = manager.delete_role(role_id=created.id)
    assert deleted.id == created.id
    assert manager.get_all_roles() == []
