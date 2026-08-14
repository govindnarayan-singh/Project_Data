"""Controlled ingestion of frozen, verified external documents."""

from .service import FROZEN_MANIFEST_SHA256, ExternalIngestionService

__all__ = ["FROZEN_MANIFEST_SHA256", "ExternalIngestionService"]
