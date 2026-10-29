"""Persistente lokale Einstellungen und atomare Ein-/Auscheck-Aktionen."""

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
import holidays

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
    """Feste Tageswarnung für berechnete Nettozeit, unabhängig vom Kalendertag."""
    target_minutes = weekly_hours * 60 // 5
    color = "red" if today_seconds > 600 * 60 else "yellow" if today_seconds > target_minutes * 60 else "green"
    return {
        "color": color,
        "label": {"green": "Im Tagesrahmen", "yellow": "Tagessoll überschritten", "red": "Mehr als 10 Stunden netto berechnet"}[color],
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


def day_totals(intervals: list[tuple[datetime, datetime]]) -> dict:
    """Berechneter Pausenabzug, keine erfundenen tatsächlichen Pausenbuchungen.

    Mindestabzug so bestimmen, dass die resultierende Nettozeit die Schwellen
    6/9 Stunden berücksichtigt. An der Schwelle nur anteilig abziehen, damit
    z.B. aus 6:01 Stunden nicht plötzlich 5:31 Stunden werden.
    """
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    gross = sum(int((end - start).total_seconds()) for start, end in merged)
    gaps = [int((right[0] - left[1]).total_seconds()) for left, right in zip(merged, merged[1:])]
    credited = sum(gap for gap in gaps if gap >= 15 * 60)
    # Kürzere Lücken sind keine anrechenbare Ruhepause und setzen die
    # Warnung vor mehr als sechs Stunden ohne qualifizierende Pause nicht zurück.
    longest = streak = 0
    for index, (start, end) in enumerate(merged):
        if index and gaps[index - 1] >= 15 * 60:
            streak = 0
        streak += int((end - start).total_seconds())
        longest = max(longest, streak)
    net = gross
    if net > 6 * 3600:
        net = max(6 * 3600, gross - max(0, 30 * 60 - credited))
    if net > 9 * 3600:
        net = max(9 * 3600, gross - max(0, 45 * 60 - credited))
    required = 45 * 60 if net > 9 * 3600 else 30 * 60 if net > 6 * 3600 else 0
    return {
        "gross_seconds": gross, "gap_seconds": sum(gaps),
        "credited_break_seconds": credited, "required_break_seconds": required,
        "automatic_break_seconds": gross - net, "net_seconds": net,
        "long_work_stretch": longest > 6 * 3600,
        **{key: value // 60 for key, value in {
            "gross_minutes": gross, "gap_minutes": sum(gaps),
            "credited_break_minutes": credited, "automatic_break_minutes": gross - net,
            "net_minutes": net,
        }.items()},
    }


def daily_totals(rows, now: datetime) -> list[dict]:
    """Intervalle nach Berliner Kalendertag schneiden, Dauern immer in UTC."""
    days: dict = {}
    for row in rows:
        start = datetime.fromisoformat(row["started_at"]).astimezone(timezone.utc)
        end = min(datetime.fromisoformat(row["ended_at"]).astimezone(timezone.utc) if row["ended_at"] else now, now)
        while start < end:
            day = start.astimezone(BERLIN).date()
            midnight = datetime.combine(day + timedelta(days=1), time(), tzinfo=BERLIN).astimezone(timezone.utc)
            stop = min(end, midnight)
            bucket = days.setdefault(day, {"intervals": [], "ids": set()})
            bucket["intervals"].append((start, stop))
            bucket["ids"].add(row["id"])
            start = stop
    result = []
    for day, bucket in sorted(days.items(), reverse=True):
        totals = day_totals(bucket["intervals"])
        window_start = datetime.combine(day, time(6), tzinfo=BERLIN).astimezone(timezone.utc)
        window_end = datetime.combine(day, time(22), tzinfo=BERLIN).astimezone(timezone.utc)
        window = day_totals([(max(start, window_start), min(end, window_end))
                             for start, end in bucket["intervals"]])
        # Rechnerische Zusatzpause hat keinen gespeicherten Zeitpunkt:
        # konservativ vollständig von der Zeit im Anrechnungsfenster abziehen.
        credited = max(0, window["gross_seconds"] - totals["automatic_break_seconds"])
        result.append({"date": day.isoformat(), "ids": bucket["ids"],
                       "credited_seconds": credited, **totals})
    return result


def period_balances(days: list[dict], tracking_start: date | None,
                    today: date, preferences: Preferences) -> list[dict]:
    """Periodenveränderungen ohne mehrfachen Anfangssaldo, bis einschließlich heute."""
    starts = [
        ("Woche", today - timedelta(days=today.weekday())),
        ("Monat", today.replace(day=1)),
        ("Quartal", today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1)),
        ("Jahr", today.replace(month=1, day=1)),
    ]
    calendar = holidays.Germany(subdiv="BW", years=range(today.year - 1, today.year + 1))
    limits = preferences.thresholds
    result = []
    for title, period_start in starts:
        start = max(period_start, tracking_start) if tracking_start else None
        target = 0
        day = start
        while day is not None and day <= today:
            if day.weekday() < 5 and day not in calendar:
                target += preferences.weekly_hours * 60 // 5
            day += timedelta(days=1)
        credited = sum(item["credited_seconds"] for item in days
                       if start and start.isoformat() <= item["date"] <= today.isoformat()) // 60
        balance = credited - target
        color = "red" if balance <= limits.negative_red or balance >= limits.positive_red else "yellow" if balance <= limits.negative_yellow or balance >= limits.positive_yellow else "green"
        result.append({"title": title, "start": start.isoformat() if start else None,
                       "end": today.isoformat(), "credited_minutes": credited,
                       "target_minutes": target, "balance_minutes": balance, "color": color})
    return result


def clock_state(path: Path, page: int = 1, now: datetime | None = None,
                year: int | None = None, month: int | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with connect(path) as connection:
        connection.execute("BEGIN")
        revision = connection.execute("SELECT clock_revision FROM local_settings WHERE id = 1").fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM local_clock_entries").fetchone()[0]
        # SQLite kennt Europe/Berlin nicht; Monatszuordnung daher explizit
        # aus dem lokalen Check-in-Datum statt aus dem UTC-Datumspräfix bilden.
        connection.create_function("berlin_period", 1, lambda value: datetime.fromisoformat(value).astimezone(BERLIN).strftime("%Y-%m"), deterministic=True)
        periods = [row[0] for row in connection.execute(
            "SELECT DISTINCT berlin_period(started_at) FROM local_clock_entries ORDER BY 1 DESC"
        )]
        clauses, parameters = [], []
        if year is not None:
            clauses.append("substr(berlin_period(started_at), 1, 4) = ?")
            parameters.append(f"{year:04d}")
        if month is not None:
            clauses.append("substr(berlin_period(started_at), 6, 2) = ?")
            parameters.append(f"{month:02d}")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        filtered_count = connection.execute("SELECT COUNT(*) FROM local_clock_entries" + where, parameters).fetchone()[0]
        pages = max(1, (filtered_count + 19) // 20)
        page = min(max(1, page), pages)
        rows = connection.execute("SELECT * FROM local_clock_entries" + where + " ORDER BY started_at DESC, id DESC LIMIT 20 OFFSET ?", (*parameters, (page - 1) * 20)).fetchall()
        active_row = connection.execute("SELECT * FROM local_clock_entries WHERE ended_at IS NULL").fetchone()
        today = now.astimezone(BERLIN).date()
        first_start = connection.execute("SELECT MIN(started_at) FROM local_clock_entries WHERE started_at <= ?", (now.isoformat().replace("+00:00", "Z"),)).fetchone()[0]
        tracking_start = datetime.fromisoformat(first_start).astimezone(BERLIN).date() if first_start else None
        # Ganze Tage laden, auch wenn weitere Einträge desselben Tages auf
        # anderen Verlaufsseiten liegen. Sonst würde die Pause mehrfach abgezogen.
        balance_start = min(today.replace(month=1, day=1), today - timedelta(days=today.weekday()))
        boundaries = [datetime.combine(balance_start, time(), tzinfo=BERLIN).astimezone(timezone.utc)]
        for row in rows:
            date = datetime.fromisoformat(row["started_at"]).astimezone(BERLIN).date()
            boundaries.append(datetime.combine(date, time(), tzinfo=BERLIN).astimezone(timezone.utc))
        summary_rows = connection.execute(
            "SELECT id, started_at, ended_at FROM local_clock_entries WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
            (now.isoformat().replace("+00:00", "Z"), min(boundaries).isoformat().replace("+00:00", "Z")),
        ).fetchall()

    def entry(row):
        start = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
        return {
            "id": row["id"],
            "started_at": start.astimezone(BERLIN).isoformat(),
            "ended_at": end.astimezone(BERLIN).isoformat() if end else None,
            "duration_minutes": max(0, int(((end or now) - start).total_seconds()) // 60),
            "period": start.astimezone(BERLIN).strftime("%Y-%m"),
            "date": start.astimezone(BERLIN).date().isoformat(),
        }

    days = daily_totals(summary_rows, now)
    today_summary = next((day for day in days if day["date"] == today.isoformat()), day_totals([]))
    today_seconds = today_summary["net_seconds"]
    visible_ids = {row["id"] for row in rows}
    month_days = [day for day in days if day["date"] >= today.replace(day=1).isoformat()]
    return {
        "active": entry(active_row) if active_row else None,
        "entries": [entry(row) for row in rows], "revision": revision,
        "count": count, "page": page, "pages": pages,
        "filtered_count": filtered_count, "selected_year": year, "selected_month": month,
        "years": sorted({int(period[:4]) for period in periods} | ({year} if year else set()), reverse=True),
        "filter_query": (f"&year={year}" if year is not None else "") + (f"&month={month}" if month is not None else ""),
        "first": (page - 1) * 20 + 1 if filtered_count else 0,
        "last": min(page * 20, filtered_count), "today_minutes": today_seconds // 60,
        "today_seconds": today_seconds,
        "today_summary": today_summary,
        "balances": period_balances(days, tracking_start, today, load_preferences(path)),
        "day_summaries": [day for day in days if day["ids"] & visible_ids],
        "month_minutes": sum(day["net_seconds"] for day in month_days) // 60,
        "month_automatic_break_minutes": sum(day["automatic_break_seconds"] for day in month_days) // 60,
        "today": now.astimezone(BERLIN).isoformat(),
    }