"""Migration script to copy data from SQLite to PostgreSQL for FlightPingBot."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from typing import Any

import asyncpg

from flightpingbot.database import Database
from flightpingbot.migrations import migrate

TABLE_ORDER = [
    "users",
    "user_settings",
    "user_aeroapi_credentials",
    "user_favorite_airports",
    "access_requests",
    "checks",
    "flight_observations",
    "api_requests",
    "monitor_jobs",
    "monitor_subscriptions",
    "alerts",
    "audit_events",
]

TABLES_WITH_SERIAL = [
    "access_requests",
    "checks",
    "flight_observations",
    "api_requests",
    "monitor_jobs",
    "monitor_subscriptions",
    "alerts",
    "audit_events",
]


async def migrate_data(sqlite_path: str, postgres_url: str) -> None:
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite file not found: {sqlite_path}")

    print(f"Connecting to PostgreSQL: {postgres_url.split('@')[-1]}")
    pg_db = Database(postgres_url)
    await pg_db.connect()

    try:
        print("Ensuring PostgreSQL schema is up to date...")
        await migrate(pg_db)
        print("Schema migrations applied successfully.")

        # Open SQLite connection
        print(f"Opening SQLite database: {sqlite_path}")
        sqlite_conn = sqlite3.connect(sqlite_path)
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_cur = sqlite_conn.cursor()

        clean_pg_url = postgres_url
        if "sslmode=" not in clean_pg_url and "ssl=" not in clean_pg_url:
            from urllib.parse import urlparse
            try:
                parsed = urlparse(clean_pg_url)
                if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
                    sep = "&" if "?" in clean_pg_url else "?"
                    clean_pg_url = f"{clean_pg_url}{sep}sslmode=disable"
            except Exception:
                pass

        # Connect directly with asyncpg for bulk operations
        pg_conn: asyncpg.Connection = await asyncpg.connect(clean_pg_url)
        try:
            async with pg_conn.transaction():
                for table in TABLE_ORDER:
                    # Get columns from SQLite
                    cols_info = sqlite_cur.execute(f"PRAGMA table_info({table})").fetchall()
                    if not cols_info:
                        print(f"Skipping table '{table}' (not present in SQLite)")
                        continue
                    cols = [c[1] for c in cols_info]

                    # Read all rows from SQLite
                    rows = sqlite_cur.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
                    print(f"Migrating '{table}': {len(rows)} rows found in SQLite...")

                    if not rows:
                        continue

                    # Construct INSERT statement
                    placeholders = [f"${i+1}" for i in range(len(cols))]
                    cols_str = ", ".join(cols)
                    placeholders_str = ", ".join(placeholders)
                    insert_sql = (
                        f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders_str}) "
                        f"ON CONFLICT DO NOTHING"
                    )

                    # Prepare row tuples
                    records = [tuple(row) for row in rows]
                    await pg_conn.executemany(insert_sql, records)
                    print(f"  -> Inserted {len(records)} rows into '{table}'.")

                # Advance serial sequences
                print("\nResetting PostgreSQL sequences...")
                for table in TABLES_WITH_SERIAL:
                    seq_val = await pg_conn.fetchval(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('{table}', 'id'),
                            COALESCE((SELECT MAX(id) FROM {table}), 1),
                            (SELECT COUNT(*) > 0 FROM {table})
                        );
                        """
                    )
                    print(f"  Sequence for {table}.id set to: {seq_val}")

            # Verification
            print("\n" + "=" * 50)
            print("VERIFICATION OF ROW COUNTS:")
            print("=" * 50)
            all_ok = True
            for table in TABLE_ORDER:
                sqlite_count = sqlite_cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                pg_count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                status = "MATCH" if sqlite_count == pg_count else "MISMATCH!"
                if sqlite_count != pg_count:
                    all_ok = False
                print(f"  {table:25} | SQLite: {sqlite_count:5} | Postgres: {pg_count:5} | [{status}]")

            if not all_ok:
                raise RuntimeError("Row count verification failed! See details above.")

            print("=" * 50)
            print("Migration completed successfully and verified!")
            print("=" * 50)

        finally:
            await pg_conn.close()
            sqlite_conn.close()

    finally:
        await pg_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate FlightPingBot data from SQLite to PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        default=os.getenv("FPB_DATABASE_PATH", "/opt/flightping/state/flightpingbot.sqlite3"),
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--postgres-url",
        default=os.getenv("FPB_DATABASE_URL", ""),
        help="PostgreSQL connection URL",
    )

    args = parser.parse_args()

    if not args.postgres_url:
        print("Error: --postgres-url or FPB_DATABASE_URL environment variable must be specified.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(migrate_data(args.sqlite_path, args.postgres_url))


if __name__ == "__main__":
    main()
