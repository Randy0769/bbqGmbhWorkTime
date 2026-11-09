"""Lokales Zeit-Dashboard mit Einstellungen und gespeicherten Check-ins."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import secrets

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import ValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.config import BASE_DIR, Settings, get_settings
from app import auth
from app.db import connect, init_db
from app.time_rules import BERLIN, Evaluation, Workday, evaluate
from app.local_tracker import Preferences, clock_action, clock_state, daily_watchdog, dashboard_warnings, load_preferences, save_preferences, load_edit_entry, update_entry, delete_entry, parse_entry_datetime, close_overdue_entries, create_entry


def format_duration(value: int | None, signed: bool = False) -> str:
    """Formatiert Minuten als Stunden und Minuten, optional mit positivem Vorzeichen.

    Bei None wird statt einer Dauer der Text „Nicht berechenbar“ zurückgegeben.
    """
    if value is None:
        return "Nicht berechenbar"
    sign = "−" if value < 0 else "+" if signed and value > 0 else ""
    hours, minutes = divmod(abs(value), 60)
    return f"{sign}{hours} h {minutes:02d} min"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Erstellt die FastAPI-Anwendung mit Anmeldung, Formularseiten und Regelprüfung.

    Ohne übergebene Einstellungen wird die Umgebungskonfiguration verwendet.
    Die Datenbankinitialisierung erfolgt erst beim Start des Anwendungslebenszyklus.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """Initialisiert die Datenbank und verwaltet den Hintergrundmonitor bis zum Herunterfahren."""
        init_db(settings.database_path)
        close_overdue_entries(settings.database_path)
        stopped = asyncio.Event()

        async def enforce_work_limit():
            """Prüft alle zehn Sekunden fällige Abschlüsse bis zum gesetzten Stoppsignal.

            Fehler beim Abschluss werden protokolliert; der Monitor prüft danach weiter.
            """
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=10)
                except TimeoutError:
                    try:
                        # Datenbankarbeit auslagern, damit sie andere Webanfragen nicht blockiert.
                        await run_in_threadpool(close_overdue_entries, settings.database_path)
                    except Exception:
                        logging.getLogger(__name__).exception("Automatischer 24-Stunden-Abschluss fehlgeschlagen")

        monitor = asyncio.create_task(enforce_work_limit())
        try:
            yield
        finally:
            # Den Monitor vor dem Herunterfahren beenden und sein Ende abwarten.
            stopped.set()
            await monitor

    application = FastAPI(title=settings.title, version="0.3.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    application.state.settings = settings
    application.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.filters["duration"] = format_duration
    # Bereits lokalisierte ISO-Werte fuer Datum, Wochentag und Uhrzeit formatieren.
    templates.env.filters["local_datetime"] = lambda value: datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M %z")
    templates.env.filters["local_date"] = lambda value: datetime.fromisoformat(value).strftime("%d.%m.%Y")
    templates.env.filters["weekday"] = lambda value: ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][datetime.fromisoformat(value).weekday()]
    templates.env.filters["local_time"] = lambda value: datetime.fromisoformat(value).strftime("%H:%M")
    labels = {
        "violation": "Verletzung", "warning": "Warnung", "info": "Hinweis",
        "unchecked": "Nicht vollständig geprüft", "incomplete": "Teilprüfung",
        "green": "Grün", "yellow": "Gelb", "red": "Rot",
    }

    @application.middleware("http")
    async def require_login(request: Request, call_next):
        """Prüft Sitzungen für geschützte Routen und setzt Schutzheader auf dynamische Antworten."""
        # Statische Ressourcen bleiben auch zum Darstellen der Anmeldeseite erreichbar.
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        token = request.cookies.get(auth.COOKIE_NAME, "")
        session, username = await run_in_threadpool(auth.auth_state, settings.database_path, token)
        request.state.session = session
        request.state.username = username
        public = request.url.path in ("/login", "/setup")
        if not public and not (session and session["authenticated"]):
            if request.method in ("GET", "HEAD"):
                response = RedirectResponse("/login" if username else "/setup", status_code=303)
            else:
                response = HTMLResponse("Anmeldung erforderlich. Bitte neu anmelden.", status_code=401)
            response.headers["Cache-Control"] = "no-store"
            return response
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def verify_csrf(form, request):
        """Prüft das Formular-Token gegen die Sitzung und lehnt Abweichungen mit HTTP 403 ab."""
        # Das Formular muss zu der Sitzung gehören, aus der es geöffnet wurde.
        session = request.state.session
        if not session or not secrets.compare_digest(str(form.get("csrf_token", "")), session["csrf_token"]):
            raise HTTPException(status_code=403, detail="Ungültiges Formular. Seite neu laden.")

    def common_context(request):
        """Liefert gemeinsame Anzeigeangaben, Anmeldestatus und Formular-Token für Templates."""
        session = request.state.session
        return {"title": settings.title, "labels": labels,
            "csrf_token": session["csrf_token"] if session else "",
            "authenticated": bool(session and session["authenticated"]), "username": request.state.username,
            "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]}

    def set_session_cookie(response, request, token):
        """Setzt das Sitzungstoken als geschütztes Cookie und gibt die Antwort zurück."""
        # HttpOnly sperrt JavaScript-Zugriff; Secure gilt nur bei einer HTTPS-Verbindung.
        response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_SECONDS,
                            httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return response

    def login_response(request, setup=False, errors=None, status_code=200):
        """Erstellt die Anmelde- oder Einrichtungsseite mit Fehlern und Formularschutz.

        Falls nötig, wird eine nicht angemeldete Sitzung angelegt und als Cookie gesetzt.
        """
        token = None
        if not request.state.session:
            # Auch das Anmeldeformular benötigt eine Sitzung für den Formularschutz.
            token = auth.anonymous_session(settings.database_path)
            request.state.session, _ = auth.auth_state(settings.database_path, token)
        response = templates.TemplateResponse(
            request=request, name="login.html", status_code=status_code,
            context={**common_context(request), "setup": setup, "errors": errors or []},
        )
        return set_session_cookie(response, request, token) if token else response

    @application.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page(request: Request):
        """Zeigt die erstmalige Einrichtung oder leitet bei vorhandenem Profil zur Anmeldung."""
        if request.state.username:
            return RedirectResponse("/login", status_code=303)
        return login_response(request, setup=True)

    @application.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request):
        """Zeigt die Anmeldung oder leitet abhängig von Profil und Sitzung passend weiter."""
        if not request.state.username:
            return RedirectResponse("/setup", status_code=303)
        if request.state.session and request.state.session["authenticated"]:
            return RedirectResponse("/", status_code=303)
        return login_response(request)

    @application.post("/setup", response_class=HTMLResponse, include_in_schema=False)
    @application.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def sign_in(request: Request):
        """Verarbeitet Einrichtung oder Anmeldung und setzt bei Erfolg eine neue Sitzung.

        Fehler erscheinen im Formular; eine aktive Anmeldesperre ergibt HTTP 429.
        """
        setup = request.url.path == "/setup"
        if setup and request.state.username:
            return RedirectResponse("/login", status_code=303)
        if not setup and not request.state.username:
            return RedirectResponse("/setup", status_code=303)
        async with request.form(max_fields=5, max_files=0) as form:
            verify_csrf(form, request)
            username = str(form.get("username", ""))
            password = str(form.get("password", ""))
            confirmation = str(form.get("confirmation", ""))
        try:
            if setup:
                token = await run_in_threadpool(auth.setup_account, settings.database_path, username, password, confirmation)
            else:
                token = await run_in_threadpool(auth.login, settings.database_path, username, password, request.cookies.get(auth.COOKIE_NAME, ""))
        except ValueError as error:
            return await run_in_threadpool(login_response, request, setup, [str(error)],
                                           429 if isinstance(error, auth.LoginBlocked) else 422 if setup else 401)
        return set_session_cookie(RedirectResponse("/", status_code=303), request, token)

    @application.post("/logout", include_in_schema=False)
    async def sign_out(request: Request):
        """Prüft den Formularschutz, entfernt Sitzung und Cookie und leitet zur Anmeldung."""
        async with request.form(max_fields=1, max_files=0) as form:
            verify_csrf(form, request)
        await run_in_threadpool(auth.destroy_session, settings.database_path, request.cookies.get(auth.COOKIE_NAME, ""))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    def render(request, page=1, errors=None, status_code=200, year=None, month=None):
        """Erstellt das Dashboard mit gefiltertem Verlauf, Warnungen und Zeitwächter."""
        clock = clock_state(settings.database_path, page, year=year, month=month)
        preferences = load_preferences(settings.database_path)
        return templates.TemplateResponse(
            request=request, name="dashboard.html", status_code=status_code,
            context={**common_context(request), "errors": errors or [],
                     "clock": clock, "preferences": preferences,
                     "warnings": dashboard_warnings(clock, preferences),
                     "watchdog": daily_watchdog(clock["today_seconds"], preferences.weekly_hours)},
            headers={"Cache-Control": "no-store"},
        )

    def saved_defaults():
        """Lädt die gespeicherten Einstellungen als Zeichenketten für das Formular."""
        return load_preferences(settings.database_path).form_values()

    @application.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request):
        """Zeigt aktuelle Einstellungen und gegebenenfalls die Speicherbestätigung."""
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context={**common_context(request), "values": saved_defaults(), "errors": [],
                     "saved": request.query_params.get("saved") == "1"},
            headers={"Cache-Control": "no-store"},
        )

    @application.post("/settings", response_class=HTMLResponse, include_in_schema=False)
    async def update_settings(request: Request):
        """Validiert und speichert Wochenmodell und Periodengrenzen bei unveränderten Tagesgrenzen.

        Ungültige Angaben werden mit HTTP 422 im Einstellungsformular angezeigt.
        """
        async with request.form(max_fields=30, max_files=0) as form:
            verify_csrf(form, request)
            values = {key: str(form.get(key, "")) for key in Preferences().form_values()}
        try:
            preferences = Preferences.model_validate({
                "weekly_hours": int(values["weekly_hours"]),
                "holiday_region": "BW",
                "thresholds": {key: values[key] for key in ("negative_red", "negative_yellow", "positive_yellow", "positive_red")},
                "daily_thresholds": load_preferences(settings.database_path).daily_thresholds,
            })
        except (ValidationError, ValueError) as error:
            errors = [item["msg"] for item in error.errors()] if isinstance(error, ValidationError) else ["Ungültige Wochenarbeitszeit."]
            return templates.TemplateResponse(
                request=request, name="settings.html", status_code=422,
                context={**common_context(request), "values": values, "errors": errors, "saved": False},
                headers={"Cache-Control": "no-store"},
            )
        save_preferences(settings.database_path, preferences)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @application.post("/clock", include_in_schema=False)
    async def update_clock(request: Request):
        """Führt einen formulargeschützten Check-in oder Check-out mit Revisionsprüfung aus.

        Ungültige Aktionen werden als Konflikt mit HTTP 409 im Dashboard angezeigt.
        """
        async with request.form(max_fields=5, max_files=0) as form:
            verify_csrf(form, request)
            action = str(form.get("action", ""))
            revision = str(form.get("revision", ""))
        try:
            clock_action(settings.database_path, action, int(revision))
        except ValueError as error:
            return render(request, errors=[str(error)], status_code=409)
        return RedirectResponse("/", status_code=303)

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request, page: int = Query(default=1, ge=1, le=1000000),
              year: int = Query(default=0, ge=0, le=9998),
              month: int = Query(default=0, ge=0, le=12)):
        """Zeigt das Dashboard; null als Jahres- oder Monatsfilter bedeutet keine Einschränkung."""
        return render(request, page=page, year=year or None, month=month or None)

    def edit_response(request, entry_id, values, errors=None, status_code=200):
        """Bereitet Datumsfelder auf und erstellt das Formular zum Anlegen oder Bearbeiten."""
        values = dict(values)
        for key in ("started_at", "ended_at"):
            try:
                local = parse_entry_datetime(values[key])
                for part, pattern in (("date", "%Y-%m-%d"), ("hour", "%H"), ("minute", "%M")):
                    values.setdefault(key + "_" + part, local.strftime(pattern))
            except ValueError:
                pass
            try:
                values[key + "_display_date"] = datetime.strptime(values.get(key + "_date", ""), "%Y-%m-%d").strftime("%d.%m.%Y")
            except ValueError:
                values[key + "_display_date"] = "Ungültiges Datum – Formular neu öffnen."
        for key in ("started_at", "ended_at"):
            values.setdefault("pause_" + key + "_date", values.get(key + "_date", ""))
        return templates.TemplateResponse(
            request=request, name="edit_entry.html", status_code=status_code,
            context={**common_context(request), "entry_id": entry_id, "values": values, "errors": errors or []},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/clock/today", include_in_schema=False)
    def live_today():
        """Liefert Zeitwerte, Warnungen und Buchungsrevision für die Live-Aktualisierung."""
        state = clock_state(settings.database_path)
        preferences = load_preferences(settings.database_path)
        watchdog = daily_watchdog(state["today_seconds"], preferences.weekly_hours)
        watchdog["target_duration"] = format_duration(watchdog["target_minutes"])
        watchdog["value_text"] = (f"{format_duration(state['today_minutes'])} netto berechnet. "
                                  f"{watchdog['label']}. Tagessoll {watchdog['target_duration']}. Rot über 10 Stunden.")
        watchdog["long_work_stretch"] = state["today_summary"].get("long_work_stretch", False)
        return {"today_minutes": state["today_minutes"],
                "today_duration": format_duration(state["today_minutes"]),
                "month_minutes": state["month_minutes"],
                "month_duration": format_duration(state["month_minutes"]),
                "today_credited_minutes": state["today_credited_minutes"],
                "today_credited_duration": format_duration(state["today_credited_minutes"]),
                "month_credited_minutes": state["month_credited_minutes"],
                "month_credited_duration": format_duration(state["month_credited_minutes"]),
                "watchdog": watchdog,
                "warnings": dashboard_warnings(state, preferences),
                "revision": state["revision"], "active": state["active"] is not None}

    @application.get("/entries/new", response_class=HTMLResponse, include_in_schema=False)
    def new_entry_page(request: Request):
        """Öffnet ein neues Buchungsformular mit aktueller Revision und einer Stunde als Vorschlag."""
        close_overdue_entries(settings.database_path)
        with connect(settings.database_path) as connection:
            revision = connection.execute("SELECT clock_revision FROM local_settings WHERE id = 1").fetchone()[0]
        end = datetime.now(BERLIN).replace(second=0, microsecond=0)
        start = (end.astimezone(timezone.utc) - timedelta(hours=1)).astimezone(BERLIN)
        values = {"revision": str(revision), "started_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
                  "ended_at": end.strftime("%Y-%m-%dT%H:%M:%S"),
                  "started_at_offset": "", "ended_at_offset": ""}
        return edit_response(request, None, values)

    @application.post("/entries/new", response_class=HTMLResponse, include_in_schema=False)
    async def add_entry(request: Request):
        """Speichert eine manuelle Buchung aus den Formularfeldern samt optionaler Pause.

        Zeigt Eingabefehler mit HTTP 422 und Revisionskonflikte mit HTTP 409 an.
        Bei Erfolg wird zum Buchungsmonat im Verlauf weitergeleitet.
        """
        async with request.form(max_fields=24, max_files=0) as form:
            verify_csrf(form, request)
            values = {key: str(form.get(key, "")) for key in
                      ("started_at_offset", "ended_at_offset", "pause_started_at_offset", "pause_ended_at_offset", "revision")}
            for key in ("started_at", "ended_at", "pause_started_at", "pause_ended_at"):
                for part in ("date", "hour", "minute"):
                    values[key + "_" + part] = str(form.get(key + "_" + part, ""))
                values[key] = (values[key + "_date"] + "T" + values[key + "_hour"]
                               + ":" + values[key + "_minute"] + ":00"
                           if any(values[key + "_" + part] for part in
                               (("hour", "minute") if key.startswith("pause_") else ("date", "hour", "minute"))) else "")
        try:
            create_entry(settings.database_path, values)
        except (ValueError, RuntimeError) as error:
            return edit_response(request, None, values, [str(error)],
                                 status_code=409 if isinstance(error, RuntimeError) else 422)
        start = parse_entry_datetime(values["started_at"])
        return RedirectResponse(f"/?year={start.year}&month={start.month}&added=1#entries-heading", status_code=303)

    @application.get("/entries/{entry_id}/edit", response_class=HTMLResponse, include_in_schema=False)
    def edit_entry_page(request: Request, entry_id: int):
        """Zeigt einen abgeschlossenen Eintrag zum Bearbeiten; fehlende IDs ergeben HTTP 404."""
        try:
            values = load_edit_entry(settings.database_path, entry_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            return render(request, errors=[str(error)], status_code=409)
        return edit_response(request, entry_id, values)

    @application.post("/entries/{entry_id}/edit", response_class=HTMLResponse, include_in_schema=False)
    async def save_entry(request: Request, entry_id: int):
        """Speichert Buchungsänderungen und leitet bei Erfolg zum Monat des Eintrags.

        Fehlende IDs ergeben HTTP 404, ungültige Angaben HTTP 422 und
        veraltete Bearbeitungsstände HTTP 409.
        """
        async with request.form(max_fields=24, max_files=0) as form:
            verify_csrf(form, request)
            values = {key: str(form.get(key, "")) for key in
                      ("started_at", "ended_at", "started_at_offset", "ended_at_offset",
                       "pause_started_at_offset", "pause_ended_at_offset", "revision")}
            for key in ("started_at", "ended_at", "pause_started_at", "pause_ended_at"):
                if key + "_date" in form:
                    for part in ("date", "hour", "minute"):
                        values[key + "_" + part] = str(form.get(key + "_" + part, ""))
                    values[key] = (values[key + "_date"] + "T" + values[key + "_hour"]
                                   + ":" + values[key + "_minute"] + ":00"
                                if any(values[key + "_" + part] for part in
                                    (("hour", "minute") if key.startswith("pause_") else ("date", "hour", "minute"))) else "")
        try:
            update_entry(settings.database_path, entry_id, values)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, RuntimeError) as error:
            return edit_response(request, entry_id, values, [str(error)],
                                 status_code=409 if isinstance(error, RuntimeError) else 422)
        # Nach dem Speichern weiterhin den Monat des Eintrags anzeigen.
        start = parse_entry_datetime(values["started_at"])
        return RedirectResponse(f"/?year={start.year}&month={start.month}&updated=1#entries-heading", status_code=303)

    @application.get("/entries/{entry_id}/delete", response_class=HTMLResponse, include_in_schema=False)
    def delete_entry_page(request: Request, entry_id: int):
        """Zeigt Zeiten und Revision eines abgeschlossenen Eintrags zur Löschbestätigung."""
        try:
            values = load_edit_entry(settings.database_path, entry_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError:
            return render(request, errors=["Laufende Einträge können erst nach dem Auschecken gelöscht werden."], status_code=409)
        return templates.TemplateResponse(
            request=request, name="delete_entry.html",
            context={**common_context(request), "entry_id": entry_id, "values": values},
            headers={"Cache-Control": "no-store"},
        )

    @application.post("/entries/{entry_id}/delete", response_class=HTMLResponse, include_in_schema=False)
    async def remove_entry(request: Request, entry_id: int):
        """Löscht nach Formular- und Revisionsprüfung den bestätigten abgeschlossenen Eintrag."""
        async with request.form(max_fields=3, max_files=0) as form:
            verify_csrf(form, request)
            revision = str(form.get("revision", ""))
        try:
            delete_entry(settings.database_path, entry_id, revision)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, RuntimeError) as error:
            return render(request, errors=[str(error)],
                          status_code=409 if isinstance(error, RuntimeError) else 422)
        return RedirectResponse("/?deleted=1#entries-heading", status_code=303)

    @application.post("/settings/password", response_class=HTMLResponse, include_in_schema=False)
    async def update_password(request: Request):
        """Ändert das Passwort und setzt bei Erfolg ein neues Sitzungscookie.

        Fehler erscheinen auf der Einstellungsseite mit HTTP 422 oder bei Sperre HTTP 429.
        """
        async with request.form(max_fields=4, max_files=0) as form:
            verify_csrf(form, request)
            current = str(form.get("current_password", ""))
            password = str(form.get("password", ""))
            confirmation = str(form.get("confirmation", ""))
        try:
            token = await run_in_threadpool(auth.change_password, settings.database_path, current, password, confirmation)
        except ValueError as error:
            return templates.TemplateResponse(
                request=request, name="settings.html",
                status_code=429 if isinstance(error, auth.LoginBlocked) else 422,
                context={**common_context(request), "values": saved_defaults(), "errors": [], "saved": False,
                         "password_errors": [str(error)]},
            )
        return set_session_cookie(RedirectResponse("/settings?password_saved=1#password", status_code=303), request, token)

    @application.post("/api/time-rules/evaluate", response_model=Evaluation, tags=["Arbeitszeitregeln"])
    def evaluate_workday(workday: Workday) -> Evaluation:
        """Liefert die gemeinsame Regelprüfung für den validierten eingegebenen Arbeitstag."""
        return evaluate(workday)

    @application.get("/health", tags=["System"])
    def health() -> dict[str, str]:
        """Prüft die Datenbankverbindung per SQL-Abfrage und liefert bei Erfolg den Status."""
        with connect(settings.database_path) as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "ok"}

    return application


app = create_app()