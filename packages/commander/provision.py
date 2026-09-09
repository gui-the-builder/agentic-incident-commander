"""Migrate and provision the isolated lab database on initial stack startup."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from commander.config import Settings
from commander.lab_storage import DemoStore
from commander.storage import make_engine


def main() -> None:
    settings = Settings()
    command.upgrade(Config("alembic.ini"), "head")
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text("""
            GRANT SELECT, INSERT, UPDATE, DELETE ON
              demo_config, demo_jobs, demo_logs, demo_changes, demo_worker_state TO demo;
            GRANT SELECT ON
              demo_config, demo_jobs, demo_logs, demo_changes, demo_worker_state TO investigation;
            ALTER TABLE demo_config ENABLE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS demo_configuration ON demo_config;
            CREATE POLICY demo_configuration ON demo_config TO demo USING (true) WITH CHECK (true);
            DROP POLICY IF EXISTS public_configuration ON demo_config;
            CREATE POLICY public_configuration ON demo_config FOR SELECT TO investigation
              USING (key IN ('new_checkout_path', 'slow_db', 'concurrency'));
        """)
        )
    DemoStore(engine).seed()
    engine.dispose()


if __name__ == "__main__":
    main()
