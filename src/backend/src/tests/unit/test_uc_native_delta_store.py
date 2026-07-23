from src.common.uc_native.delta_store import DeltaStore


def test_insert_rows_chunks_without_existence_queries():
    store = DeltaStore.__new__(DeltaStore)
    statements = []
    store.table_fqn = lambda table_name: f"catalog.schema.{table_name}"
    store.execute = statements.append

    store.insert_rows(
        "assets",
        [
            {
                "id": str(index),
                "name": f"asset_{index}",
                "asset_type_name": "Table",
                "status": "active",
                "snapshot_json": "{}",
            }
            for index in range(51)
        ],
        chunk_size=25,
    )

    assert len(statements) == 3
    assert all(statement.startswith("INSERT INTO catalog.schema.assets") for statement in statements)
    assert all("SELECT" not in statement for statement in statements)
