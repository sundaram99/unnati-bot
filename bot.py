"""
bot.py — Unnati CRM Telegram Bot entry point.

Wires up all handlers and starts the PTB application.
Run: python bot.py
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()  # must run before any module that reads os.environ at import time
print("TOKEN:", os.getenv("TELEGRAM_BOT_TOKEN"))

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import handlers
import scheduler as sched

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    level=logging.INFO,
)
# Reduce noise from httpx / httpcore
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── App factory ──────────────────────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    """Start APScheduler after the PTB event loop is running."""
    s = sched.build_scheduler(app.bot)
    s.start()
    app.bot_data["scheduler"] = s
    logger.info("Scheduler started. Jobs: %s", [j.id for j in s.get_jobs()])


async def _on_shutdown(app: Application) -> None:
    """Gracefully stop the scheduler."""
    s = app.bot_data.get("scheduler")
    if s and s.running:
        s.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def build_app() -> Application:
    """
    Create the PTB Application and register every handler.
    Handler registration order matters — more specific filters first.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    app = (
        Application.builder()
        .token(token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # ── Conversation handlers (must come before generic message handlers) ──
    app.add_handler(handlers.build_addcontact_handler())
    app.add_handler(handlers.build_addnote_handler())

    # ── Inline keyboard callbacks ──
    app.add_handler(
        CallbackQueryHandler(handlers.handle_lead_callback,  pattern="^lead_confirm_")
    )
    # ── Simple commands ──
    app.add_handler(CommandHandler("start",   handlers.start))
    app.add_handler(CommandHandler("help",    handlers.help_cmd))
    app.add_handler(CommandHandler("pipeline", handlers.pipeline))
    app.add_handler(CommandHandler("context", handlers.context_cmd))
    app.add_handler(CommandHandler("won",     handlers.won))
    app.add_handler(CommandHandler("lost",    handlers.lost))
    app.add_handler(CommandHandler("digest",     handlers.digest_cmd))
    app.add_handler(CommandHandler("nudge",      handlers.nudge_cmd))
    app.add_handler(CommandHandler("ask",        handlers.ask_cmd))
    app.add_handler(CommandHandler("createteam", handlers.createteam_cmd))
    app.add_handler(CommandHandler("jointeam",   handlers.jointeam_cmd))
    app.add_handler(CommandHandler("myteam",     handlers.myteam_cmd))
    app.add_handler(CommandHandler("remind",     handlers.remind_cmd))

    # ── Message handlers ──
    # Forwarded text/photo messages (has forward_origin attribute in PTB v21)
    app.add_handler(
        MessageHandler(
            filters.FORWARDED & (filters.TEXT | filters.PHOTO | filters.Document.ALL),
            handlers.handle_forwarded,
        )
    )

    # Voice notes
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handlers.handle_voice)
    )

    # Plain text messages — AI lead extraction (after conversations so they take priority)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_plain_text)
    )

    # Catch-all for unknown commands (must be last)
    app.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_cmd))

    return app


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Starting Unnati CRM bot…")
    app = build_app()

    # run_polling blocks until Ctrl-C; drop_pending_updates=True skips queued
    # messages that arrived while the bot was offline (avoids stale lead extractions).
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
