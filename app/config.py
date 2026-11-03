"""Konfiguration unabhängig vom aktuellen Arbeitsverzeichnis."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_path: Path
    title: str = "BBQ TimeTracker"


# Den persistenten Datenbankpfad passend zu Umgebung und Startart bestimmen.
def get_settings() -> Settings:
    frozen = getattr(sys, "frozen", False)
    # Ein expliziter Datenbankpfad hat Vorrang vor Quell- und EXE-Standardpfaden.
    override = os.environ.get("TIMETRACKER_DB_PATH")
    if override is not None:
        path = Path(override)
    elif frozen:
        # One-file-Pakete werden temporär entpackt: Nutzdaten dort niemals ablegen.
        local_data = os.environ.get("LOCALAPPDATA")
        data_dir = (Path(local_data) if local_data else Path.home() / "AppData" / "Local") / "BBQ TimeTracker" / "Version10"
        path = data_dir / "timetracker.sqlite3"
    else:
        path = BASE_DIR / "data" / "timetracker.sqlite3"
    if not path.is_absolute():
        # Quellstart: relativ zum Projekt; EXE: explizite Pfade relativ zum Aufrufordner.
        path = (Path.cwd() if frozen else BASE_DIR) / path
    return Settings(database_path=path.resolve())