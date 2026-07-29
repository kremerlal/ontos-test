"""UC-native session plumbing and group-list parsing."""

from src.common.config import parse_group_list
from src.common.database import NoOpDbSession, get_db, oltp_database_required
from src.common.uc_native.feature_managers import UcNativeTeamsManager


def test_parse_group_list_json_array():
    assert parse_group_list('["admins", "users"]') == ["admins", "users"]


def test_parse_group_list_bracketed_unquoted_shell_form():
    assert parse_group_list("[admins]") == ["admins"]


def test_parse_group_list_csv():
    assert parse_group_list("admins, users") == ["admins", "users"]


def test_get_db_yields_noop_when_oltp_not_required(monkeypatch):
    monkeypatch.setattr("src.common.database._SessionLocal", None)
    monkeypatch.setattr("src.common.database.oltp_database_required", lambda: False)
    session = next(get_db())
    assert isinstance(session, NoOpDbSession)
    session.commit()
    session.rollback()
    session.close()


def test_oltp_database_required_false_for_uc_native_settings():
    from src.common.config import Settings
    from src.common.storage_mode import resolve_storage_mode, requires_oltp_database

    settings = Settings(
        DATABRICKS_HOST="https://example.cloud.databricks.com",
        DATABRICKS_WAREHOUSE_ID="wh1",
        DATABRICKS_CATALOG="app_data",
        APP_AUDIT_LOG_DIR="audit_logs",
        STORAGE_MODE="uc_native",
        ENV="PROD",
    )
    mode = resolve_storage_mode(settings)
    assert requires_oltp_database(mode) is False


def test_uc_native_teams_manager_returns_team_read():
    class _FakeEntities:
        def list_entities(self, table_name, limit=100):
            return [
                {
                    "id": "team-1",
                    "name": "Hotel Sales Team",
                    "domain_id": "domain-1",
                    "members": [],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "created_by": "tester@example.com",
                    "updated_by": "tester@example.com",
                }
            ]

    manager = UcNativeTeamsManager(_FakeEntities())
    teams = manager.get_all_teams(db=None)
    assert len(teams) == 1
    assert teams[0].name == "Hotel Sales Team"
    assert teams[0].id == "team-1"


def test_maturity_evaluator_returns_an_empty_report_without_postgres():
    """Regression: the product page 500'd on NoOpDbSession having no .get."""
    from src.controller.maturity_evaluator import MaturityEvaluator

    report = MaturityEvaluator().evaluate(
        NoOpDbSession(), entity_type="DataProduct", entity_id="p1"
    )

    assert report is not None
    assert report.entity_type == "DataProduct"
    assert report.entity_id == "p1"
    assert report.total_levels == 0
    # The UI reads report.levels unconditionally, so it must be a list.
    assert report.levels == []


def test_maturity_evaluator_still_rejects_unknown_entity_types():
    from src.controller.maturity_evaluator import MaturityEvaluator

    assert (
        MaturityEvaluator().evaluate(
            NoOpDbSession(), entity_type="Nonsense", entity_id="x"
        )
        is None
    )
