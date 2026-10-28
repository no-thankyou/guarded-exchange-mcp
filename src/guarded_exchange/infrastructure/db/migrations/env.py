"""Alembic environment. Migrations run only through ``guarded-exchange init`` (PLAN D-11).

``infrastructure.db.migrate.upgrade_to_head`` passes an open connection (with our SQLite
pragmas and explicit ``BEGIN IMMEDIATE``) in ``config.attributes["connection"]``; there is no
``alembic.ini`` and no offline mode.
"""

from alembic import context

from guarded_exchange.infrastructure.db.schema import metadata

connection = context.config.attributes.get("connection")
if connection is None or context.is_offline_mode():
    raise RuntimeError("run migrations with `guarded-exchange init`, not the alembic CLI")

context.configure(
    connection=connection,
    target_metadata=metadata,
    render_as_batch=True,  # SQLite has almost no ALTER TABLE; batch mode = copy-and-move
    # The caller runs everything in one explicit BEGIN IMMEDIATE transaction with the driver's
    # legacy autocommit disabled, so DDL is transactional here: all steps commit or none.
    transactional_ddl=True,
)
with context.begin_transaction():
    context.run_migrations()
