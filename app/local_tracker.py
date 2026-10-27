"""Persistente lokale Einstellungen und atomare Ein-/Auscheck-Aktionen."""

import json
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.db import connect
from app.time_rules import BERLIN, StrictModel, Thresholds


class HalfHourThresholds(Thresholds):
    @model_validator(mode="after")
    def validate_half_hours(self):
        if any(value % 30 != 0 for value in self.model_dump().values()):
            raise ValueError("Gleitzeitgrenzen sind nur in halben Stunden (30-Minuten-Schritten) möglich.")
        return self


class Preferences(StrictModel):
    weekly_hours: Literal[30, 35, 40] = 40
    holiday_region: Literal["BW"] = "BW"
    opening_balance_minutes: int = Field(default=0, ge=-600000, le=600000)
    thresholds: HalfHourThresholds = Field(default_factory=HalfHourThresholds)
    daily_thresholds: HalfHourThresholds = Field(default_factory=lambda: HalfHourThresholds(
        negative_red=-120, negative_yellow=-60, positive_yellow=60, positive_red=120,
    ))

    def form_values(self) -> dict[str, str]:
        return {
            "weekly_hours": str(self.weekly_hours),
            "opening_balance_minutes": str(self.opening_balance_minutes),
            **{key: str(value) for key, value in self.thresholds.model_dump().items()},
        }


def daily_watchdog(today_seconds: int, weekly_hours: int) -> dict:
    """Feste Tageswarnung für erfasste Bruttozeit, unabhängig vom Kalendertag."""
    target_minutes = weekly_hours * 60 // 5
    color = "red" if today_seconds > 600 * 60 else "yellow" if today_seconds > target_minutes * 60 else "green"
    return {
        "color": color,
        "label": {"green": "Im Tagesrahmen", "yellow": "Tagessoll überschritten", "red": "Mehr als 10 Stunden erfasst"}[color],
        "target_minutes": target_minutes,
        "target_percent": target_minutes / 600 * 100,
        "fill_percent": min(100, max(0, today_seconds / (600 * 60) * 100)),
    }


def load_preferences(path: Path) -> Preferences:
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM local_settings WHERE id = 1").fetchone()
    return Preferences(
        weekly_hours=row["weekly_hours"], holiday_region="BW",
        opening_balance_minutes=row["opening_balance_minutes"],
        thresholds=HalfHourThresholds(**{key: row[key] for key in Thresholds.model_fields}),
        daily_thresholds=HalfHourThresholds(**{key: row[f"daily_{key}"] for key in Thresholds.model_fields}),
    )


def save_preferences(path: Path, preferences: Preferences) -> None:
    values = preferences.model_dump(exclude={"thresholds", "daily_thresholds"})
    values.update(preferences.thresholds.model_dump())
    values.update({f"daily_{key}": value for key, value in preferences.daily_thresholds.model_dump().items()})
    with connect(path) as connection:
        connection.execute(
            "UPDATE local_settings SET " + ", ".join(f"{key} = ?" for key in values) + " WHERE id = 1",
            tuple(values.values()),
        )


def clock_action(path: Path, action: str, revision: int, now: datetime | None = None) -> None:
    if action not in ("check_in", "check_out"):
        raise ValueError("Ungültige Aktion.")
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM local_settings WHERE id = 1").fetchone()
        if revision != current["clock_revision"]:
            raise ValueError("Die Zeiterfassung wurde bereits geändert. Bitte den aktuellen Status prüfen.")
        active = connection.execute("SELECT * FROM local_clock_entries WHERE ended_at IS NULL").fetchone()
        if action == "check_in":
            if active:
                raise ValueError("Sie sind bereits eingecheckt.")
            last_end = connection.execute("SELECT MAX(ended_at) FROM local_clock_entries").fetchone()[0]
            if last_end and timestamp < last_end:
                raise ValueError("Die Systemzeit liegt vor dem letzten Arbeitsende. Windows-Uhrzeit prüfen.")
            snapshot = {key: current[key] for key in current.keys() if key not in ("id", "clock_revision")}
            snapshot["holiday_region"] = "BW"
            connection.execute(
                "INSERT INTO local_clock_entries (started_at, settings_json) VALUES (?, ?)",
                (timestamp, json.dumps(snapshot)),
            )
        else:
            if not active:
                raise ValueError("Sie sind nicht eingecheckt.")
            if timestamp <= active["started_at"]:
                raise ValueError("Auschecken ist erst nach dem Check-in möglich. Bitte kurz warten oder die Systemzeit prüfen.")
            connection.execute("UPDATE local_clock_entries SET ended_at = ? WHERE id = ?", (timestamp, active["id"]))
        connection.execute("UPDATE local_settings SET clock_revision = clock_revision + 1 WHERE id = 1")


def clock_state(path: Path, page: int = 1) -> dict:
    now = datetime.now(timezone.utc)
    with connect(path) as connection:
        connection.execute("BEGIN")
        revision = connection.execute("SELECT clock_revision FROM local_settings WHERE id = 1").fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM local_clock_entries").fetchone()[0]
        pages = max(1, (count + 19) // 20)
        page = min(max(1, page), pages)
        rows = connection.execute("SELECT * FROM local_clock_entries ORDER BY started_at DESC, id DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,)).fetchall()
        active_row = connection.execute("SELECT * FROM local_clock_entries WHERE ended_at IS NULL").fetchone()
        today = now.astimezone(BERLIN).date()
        month_start = datetime.combine(today.replace(day=1), time(), tzinfo=BERLIN).astimezone(timezone.utc)
        month_rows = connection.execute(
            "SELECT started_at, ended_at FROM local_clock_entries WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
            (now.isoformat().replace("+00:00", "Z"), month_start.isoformat().replace("+00:00", "Z")),
        ).fetchall()

    def entry(row):
        start = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
        return {
            "id": row["id"],
            "started_at": start.astimezone(BERLIN).isoformat(),
            "ended_at": end.astimezone(BERLIN).isoformat() if end else None,
            "duration_minutes": max(0, int(((end or now) - start).total_seconds()) // 60),
        }

    def seconds_since(boundary):
        seconds = 0
        for row in month_rows:
            start = max(datetime.fromisoformat(row["started_at"]), boundary)
            end = min(datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else now, now)
            seconds += max(0, int((end - start).total_seconds()))
        return seconds

    today_start = datetime.combine(today, time(), tzinfo=BERLIN).astimezone(timezone.utc)
    today_seconds = seconds_since(today_start)
    return {
        "active": entry(active_row) if active_row else None,
        "entries": [entry(row) for row in rows], "revision": revision,
        "count": count, "page": page, "pages": pages,
        "first": (page - 1) * 20 + 1 if count else 0,
        "last": min(page * 20, count), "today_minutes": today_seconds // 60,
        "today_seconds": today_seconds,
        "month_minutes": seconds_since(month_start) // 60, "today": now.astimezone(BERLIN).isoformat(),
    }