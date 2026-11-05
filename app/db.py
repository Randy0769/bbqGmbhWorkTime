"""SQLite-Verbindungen und Initialisierung aus einem vollstaendigen Schema."""

import logging
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_settings

SCHEMA_VERSION = 5
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@contextmanager
# Verbindung und Transaktion kapseln und die Verbindung auch bei Fehlern schliessen.
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Eine Verbindung je Operation; Commit bei Erfolg, Rollback bei Fehlern."""
    connection = sqlite3.connect(database_path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        with connection:
            yield connection
    finally:
        # Der SQLite-Kontext beendet die Transaktion, schließt aber nicht die Verbindung.
        connection.close()


def schema_matches(connection: sqlite3.Connection, schema: str) -> bool:
    """Vergleicht Tabellenstruktur, Indizes und erforderliche Einstellungszeilen mit dem Schema."""
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(schema)
        tables = expected.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        for (table,) in tables:
            for pragma in ("table_info", "foreign_key_list", "index_list"):
                query = f'PRAGMA {pragma}("{table}")'
                actual_rows = [tuple(row) for row in connection.execute(query)]
                if actual_rows != expected.execute(query).fetchall():
                    return False
        for name, sql in expected.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"):
            actual = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()
            if actual is None or " ".join(actual[0].split()).casefold() != " ".join(sql.split()).casefold():
                return False
    for table in ("local_settings", "local_auth_attempts"):
        if connection.execute(f"SELECT COUNT(*) FROM {table} WHERE id = 1").fetchone()[0] != 1:
            return False
    return True


def replace_database(database_path: Path, schema: str) -> None:
    """Ersetzt die Zieldatenbank ohne Sicherung durch eine neu erstellte leere Datenbank.

    Erstellt zunächst eine temporäre Datei mit Schema und Versionsnummer,
    ersetzt danach die Zieldatei und entfernt alte SQLite-Nebendateien.
    """
    descriptor, name = tempfile.mkstemp(prefix=database_path.name + ".", suffix=".tmp", dir=database_path.parent)
    os.close(descriptor)
    temporary_path = Path(name)
    try:
        with connect(temporary_path) as connection:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + schema
                + f"\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        os.replace(temporary_path, database_path)
        # Alte SQLite-Nebendateien duerfen nicht auf die neue Datenbank angewendet werden.
        for suffix in ("-wal", "-shm", "-journal"):
            database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)
    finally:
        temporary_path.unlink(missing_ok=True)


def init_db(database_path: Path) -> None:
    """Initialisiert die lokale Datenbank und prüft Schema und Integrität.

    Inkompatible oder beschädigte Datenbanken werden nach einer protokollierten
    Warnung ohne Sicherung ersetzt. Sperr- und Zugriffsfehler werden weitergegeben.
    Bestehende gültige Datenbanken bleiben erhalten; die Feiertagsregion wird auf BW gesetzt.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    reason = None
    if not database_path.exists():
        replace_database(database_path, schema)
    else:
        try:
            with connect(database_path) as connection:
                # Auch bei altem Schema zuerst Schreibsperren und Schreibschutz erkennen.
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                # BEGIN IMMEDIATE allein kann auch auf schreibgeschuetzten Verbindungen gelingen.
                connection.execute(f"PRAGMA user_version = {version}")
                if version != SCHEMA_VERSION:
                    reason = f"Schemaversion {version} statt {SCHEMA_VERSION}"
                elif not schema_matches(connection, schema):
                    reason = "Datenbankschema oder erforderliche Einstellungszeilen fehlen"
                elif [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                    reason = "Datenbankintegritaet verletzt"
                elif connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    reason = "Ungueltige Fremdschluessel"
        except sqlite3.DatabaseError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                # Sperren, Zugriffs- und andere Betriebsfehler nicht als Datenverlust behandeln.
                logging.getLogger(__name__).exception("Datenbankpruefung fehlgeschlagen; Datenbank bleibt unveraendert")
                raise
            reason = str(error)
        if reason is not None:
            logging.getLogger(__name__).warning(
                "Datenbank %s wird ohne Sicherung durch eine leere ersetzt: %s", database_path, reason,
            )
            replace_database(database_path, schema)
    with connect(database_path) as connection:
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