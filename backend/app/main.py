"""FastAPI entry point for DPM platform."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure backend directory is in sys.path
_backend_dir = str(Path(__file__).resolve().parents[1])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes_audit import router as audit_router
from app.api.routes_book import router as book_router
from app.api.routes_bootstrap import router as bootstrap_router
from app.api.routes_io import router as io_router
from app.api.routes_perf import router as perf_router
from app.api.routes_plans import router as plans_router
from app.core.config import settings
from app.core.db import init_db
from app.core.errors import DpmError
from app.core.security import LocalSecurityMiddleware
from app.ui.routes_ui import router as ui_router

logger = logging.getLogger("dpm")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialise SQLite database & triggers
    init_db()
    if settings.local_token:
        print(f"\n[DPM Security] Local API token active: {settings.local_token}\n", flush=True)
    yield
    # Shutdown


app = FastAPI(
    title="DPM Platform",
    description="Lightweight Discretionary Portfolio Management System",
    version="1.1.0",
    lifespan=lifespan,
)


# Exception handlers ensuring uniform error schema: {"error": {"code": "...", "detail": "..."}}
@app.exception_handler(DpmError)
async def dpm_exception_handler(request: Request, exc: DpmError):
    return JSONResponse(
        status_code=exc.status,
        content=exc.as_dict(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "detail": str(exc),
                "meta": {"errors": exc.errors()},
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "detail": exc.detail}},
    )


# Security middleware: reject untrusted Host, reject untrusted Origin on write requests
app.add_middleware(LocalSecurityMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://{h}" for h in settings.hosts] + [f"http://{h}:8000" for h in settings.hosts],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register REST and UI routers
app.include_router(bootstrap_router)
app.include_router(book_router)
app.include_router(perf_router)
app.include_router(plans_router)
app.include_router(io_router)
app.include_router(audit_router)
app.include_router(ui_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app.main:app", host=settings.bind, port=settings.port, reload=True)
