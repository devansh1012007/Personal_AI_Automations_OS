"""FastAPI ingestion edge. See :func:`paa.api.app.create_app`.

FastAPI is an optional extra (``paa[api]``). This module is import-safe without
it: ``create_app`` imports FastAPI lazily in its body, and the schemas are plain
pydantic.
"""

from __future__ import annotations

from paa.api.app import create_app

__all__ = ["create_app"]
