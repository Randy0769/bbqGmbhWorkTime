"""Web-Einstiegspunkt mit zustandsloser Arbeitszeitprüfung, noch ohne Anmeldung."""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from pydantic import ValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, Settings, get_settings
from app.db import connect, init_db
from app.time_rules import Evaluation, Workday, evaluate


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

    application = FastAPI(title=settings.title, version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    application.state.settings = settings
    application.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    templates.env.filters["duration"] = format_duration
    templates.env.filters["local_datetime"] = lambda value: datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M %z")
    defaults = {
        "started_at": "", "ended_at": "", "previous_ended_at": "",
        "weekly_hours": "40", "completed": "on",
        "opening_balance_minutes": "0", "negative_red": "-1200",
        "negative_yellow": "-600", "positive_yellow": "600", "positive_red": "1200",
    }
    labels = {
        "violation": "Verletzung", "warning": "Warnung", "info": "Hinweis",
        "unchecked": "Nicht vollständig geprüft", "incomplete": "Teilprüfung",
        "green": "Grün", "yellow": "Gelb", "red": "Rot",
    }

    def render(request, values, pauses, result=None, errors=None, status_code=200):
        return templates.TemplateResponse(
            request=request, name="index.html", status_code=status_code,
            context={"title": settings.title, "values": values, "pauses": pauses,
                     "result": result, "errors": errors or [], "labels": labels},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        return render(request, defaults.copy(), [])

    @application.post("/", response_class=HTMLResponse, include_in_schema=False)
    async def submit_workday(request: Request):
        async with request.form(max_fields=500, max_files=0) as form:
            values = {key: str(form.get(key, "")) for key in defaults}
            try:
                count = int(str(form.get("break_count", "0")))
                if not 0 <= count <= 100:
                    raise ValueError
            except ValueError:
                return render(request, values, [], errors=["Ungültige Pausenanzahl (0–100)."], status_code=422)
            pauses = [
                {"started_at": str(form.get(f"break_start_{index}", "")),
                 "ended_at": str(form.get(f"break_end_{index}", "")),
                 "planned": form.get(f"break_planned_{index}") == "on"}
                for index in range(count)
            ]
            action = str(form.get("action", "evaluate"))
        if action == "add_break":
            if count >= 100:
                return render(request, values, pauses, errors=["Höchstens 100 Pausen möglich."], status_code=422)
            pauses.append({"started_at": "", "ended_at": "", "planned": False})
            return render(request, values, pauses)
        if action.startswith("remove_break_"):
            try:
                index = int(action.removeprefix("remove_break_"))
                if not 0 <= index < count:
                    raise ValueError
            except ValueError:
                return render(request, values, pauses, errors=["Ungültige Pause."], status_code=422)
            pauses.pop(index)
            return render(request, values, pauses)
        if action != "evaluate":
            return render(request, values, pauses, errors=["Ungültige Formularaktion."], status_code=422)
        payload = {
            key: values[key] for key in ("started_at", "ended_at", "weekly_hours", "opening_balance_minutes")
        }
        try:
            payload["weekly_hours"] = int(values["weekly_hours"])
        except ValueError:
            return render(request, values, pauses, errors=["Ungültige Wochenarbeitszeit."], status_code=422)
        payload.update(
            previous_ended_at=values["previous_ended_at"] or None,
            holiday_region="BW",
            completed=values["completed"] == "on", breaks=pauses,
            thresholds={key: values[key] for key in ("negative_red", "negative_yellow", "positive_yellow", "positive_red")},
        )
        try:
            workday = Workday.model_validate(payload)
        except ValidationError as error:
            errors = [f"{' / '.join(map(str, item['loc'])) or 'Arbeitstag'}: {item['msg']}" for item in error.errors()]
            return render(request, values, pauses, errors=errors, status_code=422)
        return render(request, values, pauses, result=evaluate(workday))

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