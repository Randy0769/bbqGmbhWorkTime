"""Anmeldung für ein lokales Profil mit scrypt und serverseitigen Sitzungen."""

import hashlib
import secrets
import time
from pathlib import Path

from app.db import connect

# Versionsgetrennte Cookies verhindern, dass parallele Installationen Sitzungen teilen.
COOKIE_NAME = "timetracker_v11_session"
SESSION_SECONDS = 12 * 60 * 60


class LoginBlocked(ValueError):
    """Kennzeichnet eine vorübergehende Anmeldesperre nach wiederholten Fehlversuchen."""

    pass


def validate_password(password: str, confirmation: str) -> None:
    """Prüft Passwortlänge und Wiederholung; lehnt ungültige Werte mit ValueError ab."""
    if not 6 <= len(password) <= 128:
        raise ValueError("Das Passwort muss 6 bis 128 Zeichen lang sein. Keine Vorgaben zur Zeichenwahl.")
    if password != confirmation:
        raise ValueError("Die neuen Passwörter stimmen nicht überein.")


def hash_password(password: str) -> str:
    """Erzeugt einen speicherbaren scrypt-Hash mit zufälligem Salt."""
    # Ein zufälliges Salt erzeugt auch für gleiche Passwörter unterschiedliche Hashwerte.
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=3, dklen=64, maxmem=64 * 1024 * 1024)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Vergleicht ein Passwort mit dem scrypt-Hash; ungültige Hashformate ergeben False."""
    if len(password) > 128:
        return False
    try:
        algorithm, salt, expected = stored.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2**15, r=8, p=3, dklen=64, maxmem=64 * 1024 * 1024)
        # Konstanter Zeitvergleich vermeidet einen Abbruch beim ersten abweichenden Byte.
        return secrets.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    """Liefert den SHA-256-Hash eines Sitzungstokens für Speicherung und Suche."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(connection, authenticated: bool) -> str:
    """Entfernt abgelaufene Sitzungen und gibt das Token einer neuen Sitzung zurück.

    Die übergebene Verbindung verwaltet die Transaktion. Angemeldete Sitzungen
    gelten zwölf Stunden, nicht angemeldete Sitzungen dreißig Minuten.
    """
    # Nur der Browser erhält das Sitzungstoken; die Datenbank speichert dessen Hashwert.
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    connection.execute("DELETE FROM local_sessions WHERE expires_at <= ?", (now,))
    connection.execute(
        "INSERT INTO local_sessions(token_hash, csrf_token, authenticated, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash(token), secrets.token_urlsafe(32), int(authenticated), now + (SESSION_SECONDS if authenticated else 1800)),
    )
    return token


def auth_state(path: Path, token: str) -> tuple[dict | None, str | None]:
    """Lädt die gültige Sitzung und den Profilnamen, jeweils None bei fehlendem Treffer."""
    with connect(path) as connection:
        account = connection.execute("SELECT username FROM local_auth WHERE id = 1").fetchone()
        session = connection.execute(
            "SELECT * FROM local_sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash(token), int(time.time())),
        ).fetchone() if token and len(token) <= 128 else None
    return (dict(session) if session else None, account["username"] if account else None)


def anonymous_session(path: Path) -> str:
    """Speichert eine nicht angemeldete Sitzung für den Formularschutz und liefert ihr Token."""
    with connect(path) as connection:
        return create_session(connection, False)


def destroy_session(path: Path, token: str) -> None:
    """Entfernt die zum Browser-Token gehörende Sitzung aus der lokalen Datenbank."""
    with connect(path) as connection:
        connection.execute("DELETE FROM local_sessions WHERE token_hash = ?", (token_hash(token),))


def check_throttle(connection) -> None:
    """Löst während einer aktiven Anmeldesperre LoginBlocked aus."""
    row = connection.execute("SELECT * FROM local_auth_attempts WHERE id = 1").fetchone()
    if row["blocked_until"] > int(time.time()):
        raise LoginBlocked("Zu viele Fehlversuche. Bitte in einer Minute erneut versuchen.")


def failed_attempt(connection) -> None:
    """Zählt einen Fehlversuch und setzt nach dem fünften eine einminütige Sperre."""
    connection.execute(
        "UPDATE local_auth_attempts SET failures = CASE WHEN failures >= 4 THEN 0 ELSE failures + 1 END, "
        "blocked_until = CASE WHEN failures >= 4 THEN ? ELSE 0 END WHERE id = 1",
        (int(time.time()) + 60,),
    )


def setup_account(path: Path, username: str, password: str, confirmation: str) -> str:
    """Legt das lokale Konto atomar an und liefert ein angemeldetes Sitzungstoken.

    Ungültige Zugangsdaten oder ein bereits vorhandenes Konto führen zu ValueError.
    """
    username = username.strip()
    if not 1 <= len(username) <= 64 or any(ord(char) < 32 for char in username):
        raise ValueError("Bitte einen Benutzernamen mit 1 bis 64 Zeichen eingeben.")
    validate_password(password, confirmation)
    encoded = hash_password(password)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM local_auth WHERE id = 1").fetchone():
            raise ValueError("Die Anmeldung wurde bereits eingerichtet. Bitte anmelden.")
        connection.execute("INSERT INTO local_auth(id, username, password_hash) VALUES (1, ?, ?)", (username, encoded))
        connection.execute("DELETE FROM local_sessions")
        return create_session(connection, True)


def login(path: Path, username: str, password: str, old_token: str) -> str:
    """Prüft den Zugang und ersetzt bei Erfolg die bisherige Sitzung.

    Liefert das neue Token. Fehlversuche werden gespeichert, bevor ValueError
    ausgelöst wird; eine aktive Sperre führt zu LoginBlocked.
    """
    result = None
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        check_throttle(connection)
        row = connection.execute("SELECT * FROM local_auth WHERE id = 1").fetchone()
        # Auch bei falschem Benutzernamen die Passwortprüfung ausführen.
        valid_password = verify_password(password, row["password_hash"]) if row else False
        if row and valid_password and username.strip().casefold() == row["username"].casefold():
            connection.execute("UPDATE local_auth_attempts SET failures = 0, blocked_until = 0 WHERE id = 1")
            connection.execute("DELETE FROM local_sessions WHERE token_hash = ?", (token_hash(old_token),))
            result = create_session(connection, True)
        else:
            failed_attempt(connection)
    # Erst nach dem Commit ablehnen, damit der Fehlversuch trotz Ausnahme gespeichert bleibt.
    if result is None:
        raise ValueError("Benutzername oder Passwort ist falsch.")
    return result


def change_password(path: Path, current: str, password: str, confirmation: str) -> str:
    """Ändert nach Passwortprüfung den Hash und ersetzt alle Sitzungen durch eine neue.

    Liefert das neue Sitzungstoken. Ungültige Angaben führen zu ValueError,
    eine aktive Anmeldesperre zu LoginBlocked.
    """
    validate_password(password, confirmation)
    encoded = hash_password(password)
    result = None
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        check_throttle(connection)
        row = connection.execute("SELECT password_hash FROM local_auth WHERE id = 1").fetchone()
        if row and verify_password(current, row["password_hash"]):
            connection.execute("UPDATE local_auth SET password_hash = ? WHERE id = 1", (encoded,))
            # Alle Geräte und alten Formulare abmelden; neue Sitzung für dieses Gerät.
            connection.execute("DELETE FROM local_sessions")
            connection.execute("UPDATE local_auth_attempts SET failures = 0, blocked_until = 0 WHERE id = 1")
            result = create_session(connection, True)
        else:
            failed_attempt(connection)
    # Auch ein falsches aktuelles Passwort muss den Fehlversuch dauerhaft erhöhen.
    if result is None:
        raise ValueError("Das aktuelle Passwort ist falsch.")
    return result