-- Lokales Einzelbenutzerprofil; keine Anmeldung und kein künstlicher Benutzer.
CREATE TABLE local_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    weekly_hours INTEGER NOT NULL DEFAULT 40 CHECK (weekly_hours IN (30, 35, 40)),
    holiday_region TEXT DEFAULT 'BW' CHECK (holiday_region IS NULL OR holiday_region IN (
        'BW','BY','BE','BB','HB','HH','HE','MV','NI','NW','RP','SL','SN','ST','SH','TH'
    )),
    opening_balance_minutes INTEGER NOT NULL DEFAULT 0,
    negative_red INTEGER NOT NULL DEFAULT -1200,
    negative_yellow INTEGER NOT NULL DEFAULT -600,
    positive_yellow INTEGER NOT NULL DEFAULT 600,
    positive_red INTEGER NOT NULL DEFAULT 1200,
    clock_revision INTEGER NOT NULL DEFAULT 0,
    CHECK (negative_red < negative_yellow AND negative_yellow < 0
        AND positive_yellow > 0 AND positive_yellow < positive_red)
);
INSERT INTO local_settings (id) VALUES (1);

CREATE TABLE local_clock_entries (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT CHECK (ended_at IS NULL OR ended_at > started_at),
    settings_json TEXT NOT NULL
);
CREATE UNIQUE INDEX one_local_running_entry ON local_clock_entries ((1)) WHERE ended_at IS NULL;