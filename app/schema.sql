-- Version 1. Zeitpunkte: UTC im Format YYYY-MM-DDTHH:MM:SSZ.
-- Dauern und Salden: ganze Minuten. Keine Passwörter im Klartext.

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(username)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    password_hash TEXT NOT NULL CHECK (length(trim(password_hash)) > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
    timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
    holiday_region TEXT DEFAULT 'DE-BW' CHECK (holiday_region IS NULL OR holiday_region IN (
        'DE-BW', 'DE-BY', 'DE-BE', 'DE-BB', 'DE-HB', 'DE-HH', 'DE-HE', 'DE-MV',
        'DE-NI', 'DE-NW', 'DE-RP', 'DE-SL', 'DE-SN', 'DE-ST', 'DE-SH', 'DE-TH'
    )),
    negative_red_minutes INTEGER NOT NULL DEFAULT -1200,
    negative_yellow_minutes INTEGER NOT NULL DEFAULT -600,
    positive_yellow_minutes INTEGER NOT NULL DEFAULT 600,
    positive_red_minutes INTEGER NOT NULL DEFAULT 1200,
    CHECK (negative_red_minutes < negative_yellow_minutes),
    CHECK (negative_yellow_minutes < 0),
    CHECK (positive_yellow_minutes > 0),
    CHECK (positive_yellow_minutes < positive_red_minutes)
);

CREATE TABLE work_schedules (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    valid_from TEXT NOT NULL CHECK (date(valid_from) IS NOT NULL AND valid_from = date(valid_from)),
    valid_until TEXT CHECK (valid_until IS NULL OR (
        date(valid_until) IS NOT NULL AND valid_until = date(valid_until) AND valid_until >= valid_from
    )),
    weekly_target_minutes INTEGER NOT NULL CHECK (weekly_target_minutes IN (1800, 2100, 2400)),
    UNIQUE (user_id, valid_from)
);

CREATE TABLE schedule_days (
    schedule_id INTEGER NOT NULL REFERENCES work_schedules(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6), -- Montag = 0
    target_minutes INTEGER NOT NULL CHECK (target_minutes BETWEEN 0 AND 1440),
    PRIMARY KEY (schedule_id, weekday)
);

CREATE TABLE time_entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    started_at TEXT NOT NULL CHECK (
        strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS NOT NULL
        AND started_at = strftime('%Y-%m-%dT%H:%M:%SZ', started_at)
    ),
    ended_at TEXT CHECK (ended_at IS NULL OR (
        strftime('%Y-%m-%dT%H:%M:%SZ', ended_at) IS NOT NULL
        AND ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', ended_at)
        AND ended_at > started_at
    )),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE UNIQUE INDEX one_running_entry_per_user ON time_entries(user_id) WHERE ended_at IS NULL;
CREATE INDEX time_entries_user_start ON time_entries(user_id, started_at);

CREATE TABLE breaks (
    id INTEGER PRIMARY KEY,
    time_entry_id INTEGER NOT NULL REFERENCES time_entries(id) ON DELETE RESTRICT,
    started_at TEXT NOT NULL CHECK (
        strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS NOT NULL
        AND started_at = strftime('%Y-%m-%dT%H:%M:%SZ', started_at)
    ),
    ended_at TEXT CHECK (ended_at IS NULL OR (
        strftime('%Y-%m-%dT%H:%M:%SZ', ended_at) IS NOT NULL
        AND ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', ended_at)
        AND ended_at > started_at
    ))
);

CREATE UNIQUE INDEX one_running_break_per_entry ON breaks(time_entry_id) WHERE ended_at IS NULL;
CREATE INDEX breaks_entry ON breaks(time_entry_id);

CREATE TABLE balance_adjustments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    effective_date TEXT NOT NULL CHECK (
        date(effective_date) IS NOT NULL AND effective_date = date(effective_date)
    ),
    minutes INTEGER NOT NULL CHECK (minutes != 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete')),
    before_json TEXT,
    after_json TEXT,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX audit_events_entity ON audit_events(entity_type, entity_id, created_at);