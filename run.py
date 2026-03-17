"""
Production launcher — reads all server settings from .env / environment variables.

Usage:
    python run.py            # uses UVICORN_WORKERS (default 2)
    UVICORN_WORKERS=1 python run.py   # single-process (dev/debug)

For development with auto-reload use:
    hatch run dev
    # or: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.uvicorn_workers,
        log_level=settings.log_level.lower(),
    )
