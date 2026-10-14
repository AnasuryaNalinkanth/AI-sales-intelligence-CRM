from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    activity,
    agent,
    analytics,
    auth,
    calls,
    contacts,
    customers,
    enquiries,
    followups,
    meetings,
    scheduling,
    imports,
)
from app.api.routes import calendar as calendar_routes
from app.core.config import Settings, settings
from app.core.exceptions import APIError, api_error_handler
from app.core.security import get_current_user
from app.database.health import check_database_connection

FRONTEND_DIR = Path(__file__).parent / "frontend"


def create_app(app_settings: Settings = settings) -> FastAPI:
    is_production = app_settings.environment.strip().lower() == "production"
    application = FastAPI(
        title=app_settings.app_name,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @application.middleware("http")
    async def revalidate_frontend(request: Request, call_next):
        # Without this, browsers heuristically cache pages and ES modules, then mix
        # old and new files after an update and break module imports.
        response = await call_next(request)
        if not request.url.path.startswith(app_settings.api_prefix):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    application.mount(
        "/static",
        StaticFiles(directory=FRONTEND_DIR),
        name="frontend-static",
    )

    @application.get("/", include_in_schema=False)
    def home_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    @application.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "login.html")

    @application.get("/customer-registration", include_in_schema=False)
    def customer_registration_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "customer-registration.html")

    @application.get("/customer", include_in_schema=False)
    def customer_intelligence_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "customer.html")

    @application.get("/leads", include_in_schema=False)
    def sales_leads_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "leads.html")

    @application.get("/analytics", include_in_schema=False)
    def analytics_dashboard_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "analytics.html")

    @application.get("/settings", include_in_schema=False)
    def settings_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "settings.html")

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def readiness() -> dict[str, str]:
        if not check_database_connection():
            raise APIError(
                "Database connectivity check failed.",
                status_code=503,
                code="database_unavailable",
            )
        return {"status": "ready", "database": "ok"}

    api_router = APIRouter(prefix=app_settings.api_prefix)
    api_router.include_router(auth.router)
    api_router.include_router(calendar_routes.callback_router)

    protected_dependencies = [Depends(get_current_user)]
    for router in (
        customers.router,
        activity.router,
        enquiries.router,
        enquiries.customer_router,
        contacts.router,
        contacts.customer_router,
        meetings.router,
        meetings.customer_router,
        scheduling.router,
        calls.router,
        calls.customer_router,
        followups.router,
        followups.customer_router,
        analytics.router,
        agent.router,
        imports.router,
        calendar_routes.router,
    ):
        api_router.include_router(router, dependencies=protected_dependencies)

    application.include_router(api_router)
    application.add_exception_handler(APIError, api_error_handler)
    return application


app = create_app()

