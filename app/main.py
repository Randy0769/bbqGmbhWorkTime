"""Lokales Zeit-Dashboard mit Einstellungen und gespeicherten Check-ins."""

from contextlib import asynccontextmanager
from datetime import datetime
import secrets

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import ValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, Settings, get_settings
from app.db import connect, init_db
from app.time_rules import Evaluation, Workday, evaluate
from app.local_tracker import Preferences, clock_action, clock_state, daily_watchdog, load_preferences, save_preferences


def format_duration(value: int | None, signed: bool = False) -> str:
    if value is None:
        return "Nicht berechenbar"
    sign = "−" if value < 0 else "+" if signed and value > 0 else ""
    hours, minutes = divmod(abs(value), 60)
    return f"{sign}{hours} h {minutes:02d} min"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        init_db(settings.database_path)
        yield

    application = FastAPI(title=settings.title, version="0.3.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    csrf_token = secrets.token_urlsafe(32)
    application.state.settings = settings
    application.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.filters["duration"] = format_duration
    templates.env.filters["local_datetime"] = lambda value: datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M %z")
    templates.env.filters["local_date"] = lambda value: datetime.fromisoformat(value).strftime("%d.%m.%Y")
    templates.env.filters["local_time"] = lambda value: datetime.fromisoformat(value).strftime("%H:%M:%S")
    labels = {
        "violation": "Verletzung", "warning": "Warnung", "info": "Hinweis",
        "unchecked": "Nicht vollständig geprüft", "incomplete": "Teilprüfung",
        "green": "Grün", "yellow": "Gelb", "red": "Rot",
    }

    def verify_csrf(form):
        if not secrets.compare_digest(str(form.get("csrf_token", "")), csrf_token):
            raise HTTPException(status_code=403, detail="Ungültiges Formular. Seite neu laden.")

    def common_context():
        return {"title": settings.title, "labels": labels, "csrf_token": csrf_token,
            "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]}

    def render(request, page=1, errors=None, status_code=200, year=None, month=None):
        clock = clock_state(settings.database_path, page, year=year, month=month)
        preferences = load_preferences(settings.database_path)
        return templates.TemplateResponse(
            request=request, name="dashboard.html", status_code=status_code,
            context={**common_context(), "errors": errors or [],
                     "clock": clock, "preferences": preferences,
                     "watchdog": daily_watchdog(clock["today_seconds"], preferences.weekly_hours)},
            headers={"Cache-Control": "no-store"},
        )

    def saved_defaults():
        return load_preferences(settings.database_path).form_values()

    @application.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context={**common_context(), "values": saved_defaults(), "errors": [],
                     "saved": request.query_params.get("saved") == "1"},
            headers={"Cache-Control": "no-store"},
        )

    @application.post("/settings", response_class=HTMLResponse, include_in_schema=False)
    async def update_settings(request: Request):
        async with request.form(max_fields=30, max_files=0) as form:
            verify_csrf(form)
            values = {key: str(form.get(key, "")) for key in Preferences().form_values()}
        try:
            preferences = Preferences.model_validate({
                "weekly_hours": int(values["weekly_hours"]),
                "holiday_region": "BW",
                "opening_balance_minutes": values["opening_balance_minutes"],
                "thresholds": {key: values[key] for key in ("negative_red", "negative_yellow", "positive_yellow", "positive_red")},
                "daily_thresholds": load_preferences(settings.database_path).daily_thresholds,
            })
        except (ValidationError, ValueError) as error:
            errors = [item["msg"] for item in error.errors()] if isinstance(error, ValidationError) else ["Ungültige Wochenarbeitszeit."]
            return templates.TemplateResponse(
                request=request, name="settings.html", status_code=422,
                context={**common_context(), "values": values, "errors": errors, "saved": False},
                headers={"Cache-Control": "no-store"},
            )
        save_preferences(settings.database_path, preferences)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @application.post("/clock", include_in_schema=False)
    async def update_clock(request: Request):
        async with request.form(max_fields=5, max_files=0) as form:
            verify_csrf(form)
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
        return render(request, page=page, year=year or None, month=month or None)

    @application.post("/api/time-rules/evaluate", response_model=Evaluation, tags=["Arbeitszeitregeln"])
    def evaluate_workday(workday: Workday) -> Evaluation:
        return evaluate(workday)

    @application.get("/health", tags=["System"])
    def health() -> dict[str, str]:
        with connect(settings.database_path) as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "ok"}

    return application


app = create_app()