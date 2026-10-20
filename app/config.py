"""Konfiguration unabhängig vom aktuellen Arbeitsverzeichnis."""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_path: Path
    title: str = "BBQ TimeTracker"


def get_settings() -> Settings:
    path = Path(os.environ.get("TIMETRACKER_DB_PATH", "data/timetracker.sqlite3"))
    if not path.is_absolute():
        path = BASE_DIR / path
    return Settings(database_path=path.resolve())