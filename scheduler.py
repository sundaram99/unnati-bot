"""
scheduler.py — APScheduler-based automated tasks for Unnati CRM.

Jobs:
  • Daily digest   — 8:00 AM IST
  • Inactivity nudge — 10:00 AM IST
  • /digest and /nudge commands call the per-user helpers directly.
"""

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot
from telegram.constants import ParseMode

import db

logger = logging.getLogger(__name__)

# Days of inactivity before a nudge fires, per stage
NUDGE_THRESHOLDS = {
    "Proposal Sent": 3,
    "Negotiating":   3,
    "Lead":          7,
    "Evaluating":    7,
}


# ── Per-user core logic ───────────────────────────────────────────────────────

async def send_digest_for_user(bot: Bot, user: dict) -> None:
    """Build and send the pipeline digest to a single user."""
    chat_id = user.get("telegram_chat_id")
    uid     = user.get("id")
    if not chat_id or not uid:
        return

    sb       = db.get_client()
    contacts = db.get_active_contacts(sb, uid)
    if not contacts:
        await bot.send_message(chat_id=chat_id, text="☀️ Good morning! Your pipeline is empty. Add your first deal with /addcontact")
        return

    now  = datetime.now(timezone.utc)
    hot, warm, cold, overdue = [], [], [], []

    for c in contacts:
        score = db.heat_score(c)
        label = f"{c['company']} — {c['name']}"

        if score >= 70:
            hot.append(label)
        elif score >= 40:
            warm.append(label)
        else:
            cold.append(label)

        stage     = c.get("stage", "")
        threshold = NUDGE_THRESHOLDS.get(stage)
        if threshold:
            last_str = c.get("last_updated") or c.get("added_on")
            if last_str:
                lu = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                days_stale = (now - lu).days
                if days_stale >= threshold:
                    overdue.append((label, days_stale))

    def fmt(items):
        return ", ".join(items) if items else "—"

    overdue_lines = "\n".join(
        f"• {label} — {d} days overdue" for label, d in overdue
    ) or "None"

    msg = (
        f"☀️ Good morning! Here's your pipeline for today:\n\n"
        f"🔴 Hot deals ({len(hot)}): {fmt(hot)}\n"
        f"🟡 Warm deals ({len(warm)}): {fmt(warm)}\n"
        f"⚪ Cold deals ({len(cold)}): {fmt(cold)}\n\n"
        f"⚠️ Needs follow-up today:\n{overdue_lines}\n\n"
        f"Total active deals: {len(contacts)}"
    )

    try:
        await bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        logger.error("Failed to send digest to %s: %s", chat_id, e)


async def send_nudges_for_user(bot: Bot, user: dict) -> None:
    """Send inactivity nudges for a single user's overdue contacts."""
    chat_id = user.get("telegram_chat_id")
    uid     = user.get("id")
    if not chat_id or not uid:
        return

    sb       = db.get_client()
    contacts = db.get_active_contacts(sb, uid)
    now      = datetime.now(timezone.utc)
    sent     = 0

    for c in contacts:
        stage     = c.get("stage", "")
        threshold = NUDGE_THRESHOLDS.get(stage)
        if threshold is None:
            continue

        last_str = c.get("last_updated") or c.get("added_on")
        if not last_str:
            continue

        lu = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        days_stale = (now - lu).days

        if days_stale >= threshold:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚠️ You haven't updated *{c['company']} — {c['name']}* "
                        f"in *{days_stale} days*.\n"
                        f"Last stage: {stage}. Follow up?"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except Exception as e:
                logger.error("Failed to send nudge to %s: %s", chat_id, e)

    if sent == 0:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="✅ No overdue contacts — your pipeline is up to date!"
            )
        except Exception as e:
            logger.error("Failed to send no-overdue message to %s: %s", chat_id, e)


# ── Reminders ─────────────────────────────────────────────────────────────────

async def check_and_send_reminders(bot: Bot) -> None:
    """Fire any reminders that are due. Runs every 60 seconds."""
    try:
        sb  = db.get_client()
        due = db.get_due_reminders(sb)
        for r in due:
            try:
                await bot.send_message(
                    chat_id=r["chat_id"],
                    text=f"⏰ *Reminder*\n\n{r['message']}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                db.mark_reminder_sent(sb, r["id"])
            except Exception as e:
                logger.error("Failed to send reminder %s to %s: %s", r["id"], r["chat_id"], e)
    except Exception as e:
        logger.error("Reminder check failed: %s", e)


# ── Broadcast wrappers (called by scheduler jobs) ─────────────────────────────

async def send_daily_digest(bot: Bot) -> None:
    """Send morning digest to all registered users."""
    logger.info("Running daily digest job…")
    sb    = db.get_client()
    users = db.get_all_users(sb)
    for user in users:
        await send_digest_for_user(bot, user)


async def send_inactivity_nudges(bot: Bot) -> None:
    """Send inactivity nudges to all registered users."""
    logger.info("Running inactivity nudge job…")
    sb    = db.get_client()
    users = db.get_all_users(sb)
    for user in users:
        await send_nudges_for_user(bot, user)


# ── Scheduler factory ─────────────────────────────────────────────────────────

def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Create, configure, and return the APScheduler instance (not yet started)."""
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    # 8:00 AM IST — daily digest
    scheduler.add_job(
        send_daily_digest,
        CronTrigger(hour=8, minute=0, timezone="Asia/Kolkata"),
        args=[bot],
        id="daily_digest",
        replace_existing=True,
    )

    # 10:00 AM IST — inactivity nudge sweep
    scheduler.add_job(
        send_inactivity_nudges,
        CronTrigger(hour=10, minute=0, timezone="Asia/Kolkata"),
        args=[bot],
        id="inactivity_nudge",
        replace_existing=True,
    )

    # Every 60 seconds — fire due reminders
    scheduler.add_job(
        check_and_send_reminders,
        IntervalTrigger(seconds=60),
        args=[bot],
        id="reminder_check",
        replace_existing=True,
    )

    return scheduler
