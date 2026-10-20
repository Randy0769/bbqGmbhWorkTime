"""Web-Einstiegspunkt. Noch keine produktiven Benutzer-/Zeiterfassungsrouten."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, Settings, get_settings
from app.db import connect, init_db


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        init_db(settings.database_path)
        yield

    application = FastAPI(title=settings.title, version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        return templates.TemplateResponse(
            request=request, name="index.html", context={"title": settings.title}
        )

    @application.get("/health", tags=["System"])
    def health() -> dict[str, str]:
        with connect(settings.database_path) as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "ok"}

    return application


app = create_app()