"""Daily scan schedule (IST, weekdays) via APScheduler."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .scanner import start_scan_async

log = logging.getLogger("equination.scheduler")
JOB_ID = "daily-scan"
_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def _job() -> None:
    try:
        start_scan_async(trigger="scheduled")
    except RuntimeError as e:
        log.warning("scheduled scan skipped: %s", e)


def apply_schedule() -> dict:
    """(Re)creates the cron job from the saved settings. Returns a description."""
    s = db.get_settings()
    if _scheduler.get_job(JOB_ID):
        _scheduler.remove_job(JOB_ID)
    if s.get("schedule_enabled", "1") != "1":
        return {"enabled": False, "next_run": None}
    hh, mm = (s.get("schedule_time") or "18:30").split(":")
    trig = CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm), timezone="Asia/Kolkata")
    job = _scheduler.add_job(_job, trig, id=JOB_ID, replace_existing=True, misfire_grace_time=3600)
    return {"enabled": True, "next_run": job.next_run_time.isoformat() if job.next_run_time else None}


def start() -> None:
    if not _scheduler.running:
        _scheduler.start()
    apply_schedule()


def describe() -> dict:
    job = _scheduler.get_job(JOB_ID)
    return {"enabled": job is not None, "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None}


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
