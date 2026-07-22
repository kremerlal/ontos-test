"""Shared Postgres connection helpers for Databricks workflow jobs."""

from __future__ import annotations

import os
from typing import Optional, Tuple
from uuid import uuid4

from databricks.sdk import WorkspaceClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def resolve_lakebase_identifier(
    ws_client: WorkspaceClient,
    *,
    app_name: Optional[str] = None,
    fallback_env: str = "LAKEBASE_INSTANCE_NAME",
) -> str:
    """Resolve Lakebase identifier from app resources or env (optional)."""
    app_name = app_name or os.environ.get("DATABRICKS_APP_NAME", "ontos")
    try:
        from src.common.database import get_lakebase_info

        info = get_lakebase_info(app_name, ws_client)
        if info:
            return info.identifier
    except Exception:
        pass
    return os.environ.get(fallback_env, "") or ""


def build_postgres_engine_from_env(ws_client: WorkspaceClient) -> Tuple[Engine, str]:
    """Build SQLAlchemy engine using password env vars or Lakebase OAuth."""
    storage_mode = os.environ.get("STORAGE_MODE", "").lower()
    if storage_mode == "uc_native":
        raise RuntimeError(
            "uc_native mode does not use Postgres. Jobs should write results to UC Delta."
        )

    host = os.environ.get("PGHOST") or os.environ.get("POSTGRES_HOST", "")
    database = os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB", "")
    port = os.environ.get("PGPORT", "5432")
    schema = os.environ.get("PGSCHEMA", "public")
    user = os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER", "")
    password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD", "")

    use_password = (
        os.environ.get("DB_USE_PASSWORD_AUTH", "").lower() in ("1", "true", "yes")
        or os.environ.get("STORAGE_MODE", "").lower() == "postgres"
        or bool(password)
    )

    if use_password:
        if not all([host, database, user, password]):
            raise RuntimeError(
                "Password Postgres mode requires PGHOST, PGDATABASE, PGUSER, PGPASSWORD"
            )
        url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        return engine, user

    instance_name = resolve_lakebase_identifier(ws_client)
    if not instance_name:
        raise RuntimeError(
            "No Postgres connection configured. Set PG* env vars with "
            "DB_USE_PASSWORD_AUTH=true, or attach a Lakebase resource."
        )

    is_autoscale = instance_name.startswith("projects/")
    if is_autoscale:
        cred = ws_client.postgres.generate_database_credential(endpoint=instance_name)
    else:
        cred = ws_client.database.generate_database_credential(
            request_id=str(uuid4()),
            instance_names=[instance_name],
        )

    sp_user = os.environ.get("DATABRICKS_CLIENT_ID") or ws_client.current_user.me().user_name
    url = f"postgresql+psycopg2://{sp_user}:{cred.token}@{host}:{port}/{database}?sslmode=require"
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    return engine, sp_user
