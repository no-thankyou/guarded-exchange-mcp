"""Two server processes on one SQLite file (Claude Code + Claude Desktop, PLAN ADR-007)."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PER_PROCESS = 40


@pytest.mark.req("NFR-2")
def test_two_processes_writing_concurrently_never_hit_database_is_locked(
    db_path: Path, tmp_path: Path
) -> None:
    go = tmp_path / "go"
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.integration.sqlite_writer",
                str(db_path),
                str(PER_PROCESS),
                str(go),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": ""},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    go.touch()
    results = [w.communicate(timeout=120) for w in workers]

    assert [w.returncode for w in workers] == [0, 0], [err for _, err in results]
    assert all("database is locked" not in err for _, err in results)
    conn = sqlite3.connect(db_path)
    try:
        seqs = [
            row[0] for row in conn.execute("SELECT seq FROM audit_log WHERE chain_id = 'race' ORDER BY seq")
        ]
        actors = {
            row[0] for row in conn.execute("SELECT DISTINCT actor FROM audit_log WHERE chain_id = 'race'")
        }
    finally:
        conn.close()
    assert seqs == list(range(1, 2 * PER_PROCESS + 1))  # gapless, no duplicates
    assert len(actors) == 2
