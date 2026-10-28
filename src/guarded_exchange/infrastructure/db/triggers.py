"""The exact append-only triggers the server requires on ``audit_log`` (FR-37, PLAN D-11).

The start-up check compares each trigger's stored SQL with these texts after whitespace and
case normalisation, so a weakened trigger with the same name (``WHEN 0``, ``WHERE 0`` inside the
body, a different message) is rejected. Migrations keep their own frozen copy of the SQL; the
integration tests prove that a freshly migrated database matches these constants. A migration
that recreates ``audit_log`` (SQLite batch mode) must recreate these triggers verbatim, or
change them here in the same commit.
"""

from typing import Final

APPEND_ONLY_TRIGGERS: Final[dict[str, str]] = {
    "audit_log_no_update": (
        "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    ),
    "audit_log_no_delete": (
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    ),
    # INSERT OR REPLACE / REPLACE INTO would otherwise delete the old row silently.
    "audit_log_no_replace": (
        "CREATE TRIGGER audit_log_no_replace BEFORE INSERT ON audit_log "
        "WHEN EXISTS (SELECT 1 FROM audit_log WHERE chain_id = NEW.chain_id AND seq = NEW.seq) "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    ),
}


def normalize_sql(sql: str) -> str:
    """Collapse whitespace and case so formatting differences do not matter."""
    return " ".join(sql.upper().split()).replace("( ", "(").replace(" )", ")")
