# Deploying Ontos Without Lakebase

This guide covers customers who have **Unity Catalog** (catalogs, schemas, tables,
volumes, SQL warehouse) but **no Lakebase** Postgres instance.

## Summary

| Component | Without Lakebase |
|-----------|------------------|
| UC browse, lineage, grants, tag sync | Supported (unchanged) |
| Volume file storage (docs, PDFs, audit) | Supported |
| Full CRUD (products, contracts, RBAC, workflows) | **`uc_native` profile** — UC Delta + Volumes |
| External Postgres OLTP | Optional via `postgres` profile |
| Read-only browse (no writes) | `uc_readonly` profile |

**Recommended:** deploy with **`STORAGE_MODE=uc_native`** so Ontos uses managed
Delta tables in your app catalog as the system of record. No Postgres or Lakebase
is required.

## Deployment profiles

### Profile: `uc_native` (recommended)

Use [`src/app-uc-native.yaml`](../src/app-uc-native.yaml) and
[`src/manifest-no-lakebase.yaml`](../src/manifest-no-lakebase.yaml).

**Prerequisites**

- Unity Catalog **catalog already created** (e.g. `app_data`) — the app service
  principal usually **cannot** `CREATE CATALOG` (metastore admin required)
- Schema + volume for app files (app can create the schema if it has
  `USE CATALOG` + `CREATE SCHEMA` on that catalog)
- SQL warehouse (Statement Execution API for Delta CRUD)
- App service principal grants: `USE CATALOG`, `CREATE SCHEMA`, `CREATE TABLE`
  on the app catalog; `CAN USE` on the warehouse; `WRITE VOLUME` on the volume

**Environment** (set in the `no-lakebase` target in [`src/databricks.yaml`](../src/databricks.yaml);
copying `app-uc-native.yaml` alone is not enough because the bundle `config` overrides it)

| Variable | Purpose |
|----------|---------|
| `STORAGE_MODE=uc_native` | UC Delta + Volumes as system of record |
| `APP_UC_APP_SCHEMA` | Delta schema for app tables (default `app_ontos`) |
| `APP_AUDIT_VOLUME_ONLY=true` | Audit to Volume (no Postgres rows) |
| `APP_ADMIN_DEFAULT_GROUPS` | JSON array of workspace groups seeded as Admin |
| `DATABRICKS_WAREHOUSE_ID` | SQL warehouse for Delta CRUD |

Deploy:

```bash
cd src
databricks bundle deploy -t no-lakebase
```

Edit `APP_ADMIN_DEFAULT_GROUPS` under `targets.no-lakebase.variables.app_config.env`
in `src/databricks.yaml` before deploy.

On first boot, Ontos creates Delta tables under `{catalog}.{APP_UC_APP_SCHEMA}` and
seeds RBAC roles from `data/settings.yaml` plus admin group membership from
`APP_ADMIN_DEFAULT_GROUPS`. This fixes the home-page "no role assigned" banner
when no Postgres is attached.

### Profile: `postgres` (legacy alternative)

Use [`src/app-no-lakebase.yaml`](../src/app-no-lakebase.yaml) when you prefer
external PostgreSQL for OLTP instead of UC Delta.

Set `STORAGE_MODE=postgres`, `DB_USE_PASSWORD_AUTH=true`, and `PG*` connection vars.

### Profile: `uc_readonly` (limited)

Set `STORAGE_MODE=uc_readonly` when **no Postgres** is available and you only need
read-only catalog browse. Writes are blocked.

## UC-native architecture

| Store | Role |
|-------|------|
| UC managed Delta (`app_data.app_ontos_*`) | Entity SoR: products, contracts, assets, tags, roles, settings, workflows |
| UC Volumes | Blobs: audit JSON, ontology files, wizard sessions, agreement PDFs |
| Jobs API | Heavy compute; run history polled live (optional Delta append) |
| UC Grants API | Platform ACLs on approve for access-grant requests |

**Tradeoffs (accepted):**

- No multi-row ACID across related entities — use `snapshot_json` aggregates and `etag` conflict detection
- No Alembic at runtime — schema evolution via additive Delta columns + migration Jobs
- Higher write latency vs Lakebase (warehouse statement API)

## Directory provider

In `uc_native` mode, Settings → Directory offers **Entra ID**, **Unity Catalog table**,
and **CSV file** only. The legacy Lakebase table provider is hidden.

## Capabilities API

`GET /api/storage/capabilities` returns the live matrix:

- `mode`: `uc_native`, `uc_readonly`, `postgres`, or `lakebase`
- `writes_enabled`: `true` in `uc_native`
- `uc_native_sor`: `true` when Delta is the system of record
- `available_capabilities` / `unavailable_capabilities`: feature flags for UI

## Troubleshooting

| Symptom | Check |
|---------|--------|
| "No role assigned" on home | Confirm `APP_ADMIN_DEFAULT_GROUPS` includes your workspace group; restart app so roles seed into Delta |
| Writes blocked | `uc_readonly` mode — switch to `uc_native` |
| Delta tables missing / "Failed to create catalog" | Pre-create `DATABRICKS_CATALOG` (e.g. `app_data`) as a metastore admin; grant the app SP `USE CATALOG` + `CREATE SCHEMA`. Apps cannot create catalogs. |
| OAuth / Lakebase errors | Set `STORAGE_MODE=uc_native` (no Postgres env vars needed) |
