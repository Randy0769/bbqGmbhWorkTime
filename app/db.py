"""SQLite-Verbindungen und versionierte Initialisierung (python -m app.db)."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_settings

SCHEMA_VERSION = 3
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Eine Verbindung je Operation; Commit bei Erfolg, Rollback bei Fehlern."""
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise RuntimeError(f"Nicht unterstützte Datenbankversion: {version}")
        if version != SCHEMA_VERSION:
            schema = SCHEMA_PATH.read_text(encoding="utf-8") if version == 0 else ""
            if version < 2:
                schema += "\n" + SCHEMA_PATH.with_name("migration_002.sql").read_text(encoding="utf-8")
            schema += "\n" + SCHEMA_PATH.with_name("migration_003.sql").read_text(encoding="utf-8")
            # DDL und Versionsnummer gemeinsam in einer Transaktion übernehmen.
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + schema
                + f"\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        # Aktuelle Einstellungen umstellen; historische Zeiteinträge bleiben unverändert.
        connection.execute(
            "UPDATE user_settings SET holiday_region = 'DE-BW' WHERE holiday_region IS NOT 'DE-BW'"
        )
        connection.execute(
            "UPDATE local_settings SET holiday_region = 'BW' WHERE holiday_region IS NOT 'BW'"
        )


if __name__ == "__main__":
    path = get_settings().database_path
    init_db(path)
    print(f"Datenbank initialisiert: {path}")