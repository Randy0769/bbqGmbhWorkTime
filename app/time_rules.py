"""Prüfung eines vollständigen Arbeitstags (ggf. über Mitternacht).

Regelfall für volljährige Beschäftigte, keine tariflichen/branchenspezifischen
Ausnahmen. §§ 3–5, 9 ArbZG: https://www.gesetze-im-internet.de/arbzg/
Ein Tag reicht weder für Ausgleichszeiträume noch für Nachtarbeitnehmer-Regeln.
Alle Arbeitsabschnitte dieses Arbeitstags müssen in der Eingabe enthalten sein;
Unterbrechungen werden als Pausen erfasst. Keine automatische Pausenbuchung.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import holidays
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def to_utc(value: datetime) -> datetime:
    """Naive Eingaben sind Berliner Ortszeit; DST-Lücken/-Mehrdeutigkeit ablehnen."""
    if value.tzinfo is not None:
        result = value.astimezone(UTC)
    else:
        candidates = set()
        for fold in (0, 1):
            candidate = value.replace(tzinfo=BERLIN, fold=fold).astimezone(UTC)
            if candidate.astimezone(BERLIN).replace(tzinfo=None) == value:
                candidates.add(candidate)
        if len(candidates) != 1:
            raise ValueError("Ortszeit ist wegen Zeitumstellung ungültig oder mehrdeutig; bitte UTC-Offset angeben.")
        result = candidates.pop()
    if result.second or result.microsecond:
        raise ValueError("Zeitpunkte müssen minutengenau sein (Sekunden = 0).")
    if not 2000 <= result.astimezone(BERLIN).year <= 2100:
        raise ValueError("Datum muss zwischen 2000 und 2100 liegen.")
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pause(StrictModel):
    started_at: datetime
    ended_at: datetime
    planned: bool = False

    _normalize = field_validator("started_at", "ended_at")(to_utc)

    @model_validator(mode="after")
    def validate_interval(self):
        if self.ended_at <= self.started_at:
            raise ValueError("Pausenende muss nach dem Pausenbeginn liegen.")
        return self


class Thresholds(StrictModel):
    negative_red: int = Field(default=-1200, ge=-60000, le=0)
    negative_yellow: int = Field(default=-600, ge=-60000, le=0)
    positive_yellow: int = Field(default=600, ge=0, le=60000)
    positive_red: int = Field(default=1200, ge=0, le=60000)

    @model_validator(mode="after")
    def validate_order(self):
        if not self.negative_red < self.negative_yellow < 0 < self.positive_yellow < self.positive_red:
            raise ValueError("Ampelgrenzen: Rot minus < Gelb minus < 0 < Gelb plus < Rot plus.")
        return self


class Workday(StrictModel):
    started_at: datetime
    ended_at: datetime = Field(description="Arbeitsende oder Prüfzeitpunkt bei laufender Arbeit.")
    completed: bool = True
    previous_ended_at: datetime | None = None
    breaks: list[Pause] = Field(default_factory=list, max_length=100)
    weekly_hours: Literal[30, 35, 40] = 40
    holiday_region: Literal["BW"] = "BW"
    opening_balance_minutes: int = Field(default=0, ge=-600000, le=600000)
    thresholds: Thresholds = Field(default_factory=Thresholds)

    _normalize = field_validator("started_at", "ended_at")(to_utc)

    @field_validator("previous_ended_at")
    @classmethod
    def normalize_previous(cls, value):
        return to_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_workday(self):
        if not timedelta(0) < self.ended_at - self.started_at <= timedelta(hours=24):
            raise ValueError("Ein Arbeitstag muss länger als 0 und höchstens 24 tatsächliche Stunden sein.")
        if self.previous_ended_at is not None and self.previous_ended_at > self.started_at:
            raise ValueError("Vorheriges Arbeitsende darf nicht nach dem aktuellen Beginn liegen.")
        ordered = sorted(self.breaks, key=lambda pause: pause.started_at)
        previous_end = self.started_at
        for pause in ordered:
            if not self.started_at < pause.started_at < pause.ended_at < self.ended_at:
                raise ValueError("Pausen müssen vollständig innerhalb des Arbeitstags liegen.")
            if pause.started_at <= previous_end:
                raise ValueError("Pausen dürfen sich nicht überschneiden oder direkt aneinandergrenzen; zusammenfassen.")
            previous_end = pause.ended_at
        self.breaks = ordered
        return self


class Finding(StrictModel):
    code: str
    severity: Literal["violation", "warning", "info", "unchecked"]
    message: str
    legal_basis: str | None = None


class Evaluation(StrictModel):
    actual_minutes: int
    credited_minutes: int
    outside_window_minutes: int
    break_minutes: int
    qualifying_break_minutes: int
    required_break_minutes: int
    longest_work_block_minutes: int
    rest_minutes: int | None
    earliest_next_start: str
    target_minutes: int | None
    daily_balance_minutes: int | None
    projected_balance_minutes: int | None
    balance_color: Literal["green", "yellow", "red"] | None
    status: Literal["violation", "warning", "incomplete"]
    findings: list[Finding]


def minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds()) // 60


def local_boundary(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=BERLIN).astimezone(UTC)


def evaluate(workday: Workday) -> Evaluation:
    findings: list[Finding] = []

    def add(code, severity, message, basis=None):
        findings.append(Finding(code=code, severity=severity, message=message, legal_basis=basis))

    # Tatsächliche Unterbrechungen abziehen, aber nur vorher festgelegte Pausen
    # von mindestens 15 Minuten als gesetzliche Ruhepause berücksichtigen.
    segments = []
    cursor = workday.started_at
    qualifying = 0
    block = 0
    longest = 0
    for pause in workday.breaks:
        segments.append((cursor, pause.started_at))
        block += minutes(cursor, pause.started_at)
        longest = max(longest, block)
        duration = minutes(pause.started_at, pause.ended_at)
        if pause.planned and duration >= 15:
            qualifying += duration
            block = 0
        cursor = pause.ended_at
    segments.append((cursor, workday.ended_at))
    block += minutes(cursor, workday.ended_at)
    longest = max(longest, block)
    actual = sum(minutes(start, end) for start, end in segments)
    total_break = minutes(workday.started_at, workday.ended_at) - actual
    required = 45 if actual > 540 else 30 if actual > 360 else 0

    if actual > 600:
        add("daily_max", "violation", "Mehr als 10 Stunden tatsächliche Arbeitszeit. Verletzung beim Vorgesetzten melden.", "§ 3 ArbZG")
    elif actual > 480:
        add("daily_extension", "warning", "Mehr als 8 Stunden: nur mit Ausgleich auf durchschnittlich 8 Stunden werktäglich innerhalb von 6 Kalendermonaten oder 24 Wochen zulässig.", "§ 3 ArbZG")
    if not workday.completed and 570 <= actual <= 600:
        add("daily_max_soon", "warning", f"10-Stunden-Grenze in {600 - actual} Arbeitsminuten erreicht.", "§ 3 ArbZG")
    elif not workday.completed and 450 <= actual <= 480:
        add("daily_extension_soon", "warning", f"8 Stunden in {480 - actual} Arbeitsminuten erreicht; Ausgleich einplanen.", "§ 3 ArbZG")

    if qualifying < required:
        severity = "violation" if workday.completed else "warning"
        message = f"Es fehlen {required - qualifying} Minuten anrechenbare Ruhepause (mindestens 15 Minuten je Abschnitt, im Voraus festgelegt)."
        if workday.completed:
            message += " Verletzung beim Vorgesetzten melden."
        add("break_total", severity, message, "§ 4 ArbZG")
    if longest > 360:
        add("continuous_work", "violation", "Länger als 6 Stunden ohne ausreichende Ruhepause gearbeitet. Eine spätere Pause heilt dies nicht. Verletzung beim Vorgesetzten melden.", "§ 4 ArbZG")
    elif not workday.completed and block >= 330:
        add("break_soon", "warning", f"Spätestens in {360 - block} weiteren Arbeitsminuten eine Ruhepause beginnen.", "§ 4 ArbZG")
    if total_break > qualifying:
        add("short_breaks", "info", "Nicht vorher festgelegte Unterbrechungen oder Abschnitte unter 15 Minuten wurden abgezogen, zählen aber nicht als gesetzliche Ruhepause.", "§ 4 ArbZG")

    rest = None
    if workday.previous_ended_at is None:
        add("rest_unknown", "unchecked", "Ruhezeit vor Arbeitsbeginn nicht prüfbar: vorheriges Arbeitsende fehlt.", "§ 5 ArbZG")
    else:
        rest = minutes(workday.previous_ended_at, workday.started_at)
        if rest < 660:
            add("rest_short", "violation", f"Ruhezeit nur {rest} statt mindestens 660 Minuten (11 Stunden). Verletzung beim Vorgesetzten melden; mögliche branchenspezifische Ausnahme prüfen.", "§ 5 ArbZG")

    start_day = workday.started_at.astimezone(BERLIN).date()
    last_day = workday.ended_at.astimezone(BERLIN).date()
    calendar = holidays.Germany(subdiv=workday.holiday_region, years=range(start_day.year, last_day.year + 1))
    credited = 0
    restricted_days = set()
    night_minutes = 0
    day = start_day
    while day <= last_day:
        day_start = local_boundary(day, 0)
        day_end = local_boundary(day + timedelta(days=1), 0)
        window_start, window_end = local_boundary(day, 6), local_boundary(day, 22)
        for start, end in segments:
            credited += max(0, minutes(max(start, window_start), min(end, window_end)))
            on_day = max(0, minutes(max(start, day_start), min(end, day_end)))
            if on_day and (day.weekday() == 6 or day in calendar):
                restricted_days.add(day)
            for night_start, night_end in ((day_start, local_boundary(day, 6)), (local_boundary(day, 23), day_end)):
                night_minutes += max(0, minutes(max(start, night_start), min(end, night_end)))
        day += timedelta(days=1)

    if actual > credited:
        add("outside_window", "info", f"{actual - credited} Minuten außerhalb 06–22 Uhr: nicht im betrieblichen Zeitkonto, aber vollständig in der gesetzlichen Prüfung. Vergütungsansprüche werden damit nicht bewertet.")
    for restricted_day in sorted(restricted_days):
        add("restricted_day", "violation", f"Arbeit am {restricted_day:%d.%m.%Y}: Sonn-/Feiertagsruhe im Regelfall verletzt. Beim Vorgesetzten melden; Ausnahmen und Ersatzruhetage gesondert prüfen.", "§§ 9–11 ArbZG")
    if night_minutes:
        add("night_work", "unchecked", "Arbeitszeit in der gesetzlichen Nachtzeit erfasst. Nachtarbeitnehmerstatus, besondere Ausgleichsfristen und Branchenregeln sind gesondert zu prüfen.", "§§ 2, 6 ArbZG")

    # Feiertage gelten fest für Baden-Württemberg; tagübergreifend keine fiktive Sollzeit.
    target = None
    if start_day == last_day:
        target = workday.weekly_hours * 60 // 5 if start_day.weekday() < 5 and start_day not in calendar else 0
        add("target_model", "info", "Tagessoll auf Basis einer gleichmäßigen Montag–Freitag-Woche; Urlaub, Krankheit und individuelle Verteilung sind nicht berücksichtigt.")
    else:
        add("target_unknown", "unchecked", "Tagessaldo nicht berechnet: Feiertagslage oder Verteilung über Tagesgrenzen ist nicht eindeutig.")

    delta = credited - target if target is not None else None
    projected = workday.opening_balance_minutes + delta if delta is not None else None
    color = None
    if projected is not None:
        limits = workday.thresholds
        color = "red" if projected <= limits.negative_red or projected >= limits.positive_red else "yellow" if projected <= limits.negative_yellow or projected >= limits.positive_yellow else "green"

    add("scope", "unchecked", "Prüfung nur des eingegebenen Arbeitstags im gesetzlichen Regelfall. Alle Arbeitsabschnitte und Tätigkeiten bei anderen Arbeitgebern einbeziehen. Langzeitausgleich, Sondergruppen, Tarifausnahmen und vollständige ArbZG-Konformität sind nicht geprüft.")
    status = "violation" if any(item.severity == "violation" for item in findings) else "warning" if any(item.severity == "warning" for item in findings) else "incomplete"
    return Evaluation(
        actual_minutes=actual, credited_minutes=credited,
        outside_window_minutes=actual - credited, break_minutes=total_break,
        qualifying_break_minutes=qualifying, required_break_minutes=required,
        longest_work_block_minutes=longest, rest_minutes=rest,
        earliest_next_start=(workday.ended_at + timedelta(hours=11)).astimezone(BERLIN).isoformat(),
        target_minutes=target, daily_balance_minutes=delta,
        projected_balance_minutes=projected, balance_color=color,
        status=status, findings=findings,
    )