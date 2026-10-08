from pathlib import Path
import secrets
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from . import __version__
from .db import Database
from .models import current_month, month_key
from .service import summary

STATIC = Path(__file__).parent / "static"


def create_app(config, token=None):
    db = Database(config.database)
    app = FastAPI(title="CloudCost", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    def auth(authorization: str | None = Header(default=None)):
        if token and (not authorization or not secrets.compare_digest(authorization.encode(), ("Bearer " + token).encode())):
            raise HTTPException(401, "需要有效的 Bearer token", headers={"WWW-Authenticate": "Bearer"})

    def valid_month(month: str | None):
        try:
            return month_key(month or current_month())
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health", dependencies=[Depends(auth)])
    def health():
        return {"status": "ok", "version": __version__, "demo": config.demo}

    @app.get("/api/months", dependencies=[Depends(auth)])
    def months():
        return {"months": sorted(set(db.months()) | {current_month()}, reverse=True), "current_month": current_month()}

    @app.get("/api/summary", dependencies=[Depends(auth)])
    def get_summary(month: str | None = None):
        return summary(config, db, valid_month(month))

    @app.get("/api/history", dependencies=[Depends(auth)])
    def history(month: str | None = None, provider: str | None = None, limit: int = Query(default=500, ge=1, le=5000)):
        if provider:
            try:
                config.provider(provider)
            except ValueError as e:
                raise HTTPException(404, str(e)) from None
        rows = db.history(valid_month(month), provider, limit)
        for row in rows:
            row["amount_usd_at_capture"] = row["amount_usd"]
            row["amount_usd"] = str(config.convert(row["amount"], row["currency"]))
        return {"snapshots": rows}

    @app.get("/api/alerts", dependencies=[Depends(auth)])
    def alerts(month: str | None = None, limit: int = Query(default=100, ge=1, le=1000)):
        return {"alerts": db.alerts(valid_month(month), limit)}

    @app.get("/api/collections", dependencies=[Depends(auth)])
    def collections(limit: int = Query(default=50, ge=1, le=1000)):
        return {"collections": db.collections(limit)}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
