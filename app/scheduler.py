"""Daily scan schedules (IST, weekdays), one APScheduler job per user."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .scanner import start_scan_async

log = logging.getLogger("equination.scheduler")
_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def _job_id(user_id: int) -> str:
    return f"daily-scan-{user_id}"


def _job(user_id: int) -> None:
    try:
        start_scan_async(user_id, trigger="scheduled")
    except RuntimeError as e:
        log.warning("scheduled scan skipped for user %s: %s", user_id, e)


def apply_schedule(user_id: int) -> dict:
    """(Re)creates the user's cron job from their saved settings."""
    s = db.get_settings(user_id)
    jid = _job_id(user_id)
    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)
    if s.get("schedule_enabled", "1") != "1":
        return {"enabled": False, "next_run": None}
    try:
        hh, mm = (s.get("schedule_time") or "18:30").split(":")
        trig = CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm), timezone="Asia/Kolkata")
    except ValueError:
        return {"enabled": False, "next_run": None}
    job = _scheduler.add_job(_job, trig, args=[user_id], id=jid, replace_existing=True, misfire_grace_time=3600)
    return {"enabled": True, "next_run": job.next_run_time.isoformat() if job.next_run_time else None}


def describe(user_id: int) -> dict:
    job = _scheduler.get_job(_job_id(user_id))
    return {"enabled": job is not None, "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None}


def start() -> None:
    if not _scheduler.running:
        _scheduler.start()
    for uid in db.list_user_ids():
        apply_schedule(uid)


def shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
