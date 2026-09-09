"""Wait for migrated tables and role grants before starting a Kubernetes application."""

import argparse
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from commander.config import Settings
from commander.storage import make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", choices=["incidents", "demo_config"])
    args = parser.parse_args()
    engine = make_engine(Settings().database_url)
    try:
        deadline = time.monotonic() + 300
        while True:
            try:
                with engine.connect() as connection:
                    connection.execute(text(f"SELECT 1 FROM {args.table} LIMIT 1"))
                return
            except SQLAlchemyError:
                if time.monotonic() >= deadline:
                    raise SystemExit(
                        "Database schema or role grants did not become ready"
                    ) from None
                time.sleep(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
