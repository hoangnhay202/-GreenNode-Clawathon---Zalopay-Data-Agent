"""Persist scheduled job configs in PostgreSQL for auto-restore on startup.

Every user-created job (news / excel / onedrive) is saved here so that
when the container restarts, scheduler_manager.restore_from_persist() can
recreate all jobs from the stored configs without user intervention.
"""
from __future__ import annotations

import logging

from . import pg_store

logger = logging.getLogger(__name__)


def save_job(job_id: str, job_type: str, config: dict) -> None:
    """Save job config to PostgreSQL. Overwrites silently if job_id already exists."""
    pg_store.save_job_config(job_id, job_type, config)
    logger.debug("job_persist: saved %s (%s)", job_id, job_type)


def delete_job(job_id: str) -> None:
    """Remove job config from PostgreSQL."""
    pg_store.delete_job_config(job_id)
    logger.debug("job_persist: deleted %s", job_id)


def get_all_jobs() -> list[dict]:
    """Return all persisted job configs as a list of dicts (includes job_id key)."""
    return pg_store.get_all_job_configs()
