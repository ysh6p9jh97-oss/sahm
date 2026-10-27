"""ASGI entry point:  uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import logging

from app.web import build_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = build_app()
