import json

from src.common.uc_native.delta_store import DeltaStore, _sql_literal


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


def test_sql_literal_doubles_backslashes():
    """Spark SQL interprets backslash escapes, so JSON payloads must be re-escaped.

    Without this, json.dumps output containing \\n round-tripped as a real newline
    and every snapshot_json field on the row was silently lost on read.
    """
    payload = json.dumps({"content_markdown": "# Title\nBody with \"quotes\" and a \\ slash"})
    literal = _sql_literal(payload)

    assert literal.startswith("'") and literal.endswith("'")
    # The stored characters must be a backslash-escaped copy of the JSON text.
    assert literal[1:-1] == payload.replace("\\", "\\\\")
    # Reversing Spark's own unescaping must give back parsable JSON.
    assert json.loads(literal[1:-1].replace("\\\\", "\\"))["content_markdown"].startswith("# Title\n")


def test_sql_literal_escapes_single_quotes():
    assert _sql_literal("O'Brien") == "'O''Brien'"
    assert _sql_literal(None) == "NULL"
    assert _sql_literal(True) == "true"
    assert _sql_literal(42) == "42"
