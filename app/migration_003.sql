-- Tagesgrenzen sind getrennt von den Grenzen des gesamten Zeitkontos.
ALTER TABLE local_settings ADD COLUMN daily_negative_red INTEGER NOT NULL DEFAULT -120 CHECK (daily_negative_red BETWEEN -60000 AND -30 AND daily_negative_red % 30 = 0);
ALTER TABLE local_settings ADD COLUMN daily_negative_yellow INTEGER NOT NULL DEFAULT -60 CHECK (daily_negative_yellow BETWEEN -59970 AND -30 AND daily_negative_yellow % 30 = 0);
ALTER TABLE local_settings ADD COLUMN daily_positive_yellow INTEGER NOT NULL DEFAULT 60 CHECK (daily_positive_yellow BETWEEN 30 AND 59970 AND daily_positive_yellow % 30 = 0);
ALTER TABLE local_settings ADD COLUMN daily_positive_red INTEGER NOT NULL DEFAULT 120 CHECK (daily_positive_red BETWEEN 30 AND 60000 AND daily_positive_red % 30 = 0);

-- Bestandswerte auf nächste halbe Stunde runden (15 Minuten aufwärts).
-- Warn-/Kritisch-Grenzen bleiben mindestens 30 Minuten auseinander.
-- Historische Check-in-Snapshots und erfasste Zeiten bleiben unverändert.
UPDATE local_settings SET
    negative_yellow = -min(59970, max(30, ((abs(negative_yellow) + 15) / 30) * 30)),
    negative_red = -max(
        min(60000, max(60, ((abs(negative_red) + 15) / 30) * 30)),
        min(59970, max(30, ((abs(negative_yellow) + 15) / 30) * 30)) + 30
    ),
    positive_yellow = min(59970, max(30, ((positive_yellow + 15) / 30) * 30)),
    positive_red = max(
        min(60000, max(60, ((positive_red + 15) / 30) * 30)),
        min(59970, max(30, ((positive_yellow + 15) / 30) * 30)) + 30
    );