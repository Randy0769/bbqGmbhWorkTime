-- Anmeldung für das bestehende lokale Profil; Zeiteinträge bleiben unverändert.
CREATE TABLE local_auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL
);
CREATE TABLE local_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    authenticated INTEGER NOT NULL CHECK (authenticated IN (0, 1)),
    expires_at INTEGER NOT NULL
);
CREATE TABLE local_auth_attempts (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    failures INTEGER NOT NULL DEFAULT 0,
    blocked_until INTEGER NOT NULL DEFAULT 0
);
INSERT INTO local_auth_attempts(id) VALUES (1);