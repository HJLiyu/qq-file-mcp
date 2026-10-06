import sqlite3

import pytest

from qq_file_mcp.state import ResultStore


@pytest.mark.parametrize("fail", [False, True])
def test_transactions_close_connections_and_preserve_commit_or_rollback(
    tmp_path, monkeypatch, fail
):
    connections = []
    connect = sqlite3.connect

    def retained_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)  # Keep handles alive; do not depend on garbage collection.
        return db

    monkeypatch.setattr("qq_file_mcp.state.sqlite3.connect", retained_connect)
    store = ResultStore(tmp_path / "state")
    try:
        with store._connect() as db:
            db.execute("INSERT INTO items VALUES ('test', 'test', '{}', 0)")
            if fail:
                raise ValueError("rollback")
    except ValueError:
        assert fail
    with connect(store.path) as check:
        assert check.execute("SELECT COUNT(*) FROM items").fetchone()[0] == (0 if fail else 1)
    check.close()
    try:
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
        # On Windows a still-open SQLite connection prevents this operation.
        store.path.unlink()
    finally:
        for connection in connections:
            connection.close()
