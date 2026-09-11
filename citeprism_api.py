"""ASGI entry point: ``uvicorn citeprism_api:app``."""

from src.webapi.app import app

__all__ = ["app"]
