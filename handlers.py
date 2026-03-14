"""
handlers.py — All Telegram command and message handlers for Unnati CRM.

Each handler function is registered in bot.py.
Conversation flows use PTB's ConversationHandler with per-user state.
"""

import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

import db
import ai
import scheduler as sched

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
# /addcontact flow
AC_NAME, AC_COMPANY, AC_STAGE, AC_SOURCE = range(4)

# /addnote flow
AN_CONTACT, AN_NOTE = range(10, 12)

# Stage keyboard shown during /addcontact
STAGE_OPTIONS = [
    ["Lead", "Evaluating"],
    ["Proposal Sent", "Negotiating"],
]

HELP_TEXT = (
    "🤖 Unnati CRM — Available Commands:\n\n"
    "/start — Set up your account\n"
    "/addcontact — Add a contact manually\n"
    "/pipeline — View all active deals\n"
    "/context [name] — Pre-call brief for a contact\n"
    "/won — Mark latest deal as won\n"
    "/lost — Mark latest deal as lost\n"
    "/addnote — Add a note to a contact\n"
    "/ask [question] — Ask anything about your pipeline\n"
    "/digest — Show today's pipeline digest now\n"
    "/nudge — Check for overdue contacts now\n"
    "/help — Show this message\n\n"
    "💡 Tip: Forward any WhatsApp message or paste text from a call — I'll extract the deal automatically!"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sb():
    """Shortcut: get a fresh Supabase client per request (thread-safe)."""
    return db.get_client()


def _get_team_id(sb, uid: str) -> str | None:
    """Return the team_id for this user, or None if they're solo."""
    team = db.get_user_team(sb, uid)
    return team["id"] if team else None


def _user_id_from_context(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Pull the Supabase user UUID stored in context.user_data after /start."""
    return context.user_data.get("user_id")


async def _ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """
    Make sure the user has run /start.
    Returns the Supabase user_id or sends an error and returns None.
    """
    uid = _user_id_from_context(context)
    if uid:
        return uid

    # Try loading from DB (handles bot restart)
    sb = _sb()
    user = db.get_user(sb, update.effective_chat.id)
    if user:
        context.user_data["user_id"] = user["id"]
        return user["id"]

    await update.message.reply_text(
        "👋 Please run /start first to set up your Unnati CRM account."
    )
    return None


# ── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register the user and greet them."""
    chat    = update.effective_chat
    tg_user = update.effective_user
    name    = tg_user.full_name or tg_user.username or "Founder"

    sb   = _sb()
    user = db.upsert_user(sb, chat.id, name)

    if user:
        context.user_data["user_id"] = user["id"]

    await update.message.reply_text(
        f"🚀 *Welcome to Unnati CRM, {name}!*\n\n"
        "Your Telegram-native deal tracker is ready.\n\n"
        "Quick start:\n"
        "• /addcontact — Log your first lead\n"
        "• /pipeline — View your deal pipeline\n"
        "• Forward any message — I'll extract the lead for you\n\n"
        "Type /help anytime for the full command list.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /help ────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# ── /addcontact (4-step conversation) ────────────────────────────────────────

async def addcontact_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: ask for the contact's name."""
    uid = await _ensure_user(update, context)
    if not uid:
        return ConversationHandler.END

    context.user_data["new_contact"] = {}
    await update.message.reply_text(
        "📇 *Add a new contact*\n\nStep 1/4 — What's the contact's *full name*?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    return AC_NAME


async def addcontact_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store name, ask for company."""
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Name can't be empty. Try again:")
        return AC_NAME

    context.user_data["new_contact"]["name"] = name
    await update.message.reply_text(
        f"Got it — *{name}*\n\nStep 2/4 — Which *company* are they from?",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AC_COMPANY


async def addcontact_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store company, ask for deal stage."""
    company = update.message.text.strip()
    context.user_data["new_contact"]["company"] = company

    keyboard = [
        [InlineKeyboardButton(s, callback_data=f"stage:{s}") for s in row]
        for row in STAGE_OPTIONS
    ]
    await update.message.reply_text(
        f"🏢 *{company}*\n\nStep 3/4 — What's the current *deal stage*?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return AC_STAGE


async def addcontact_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store stage (from inline button), ask for lead source."""
    query = update.callback_query
    await query.answer()
    stage = query.data.split(":", 1)[1]

    context.user_data["new_contact"]["stage"] = stage
    company = context.user_data["new_contact"].get("company", "")
    await query.edit_message_text(
        f"🏢 *{company}*\n\n📊 Stage: *{stage}* ✓",
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.message.reply_text(
        "Step 4/4 — How did you find this lead? (e.g. referral, LinkedIn, cold outreach, event)",
        reply_markup=ReplyKeyboardMarkup(
            [["Referral", "LinkedIn"], ["Cold outreach", "Event"], ["Other"]],
            one_time_keyboard=True,
            resize_keyboard=True,
        ),
    )
    return AC_SOURCE


async def addcontact_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the full contact to DB and confirm."""
    source  = update.message.text.strip()
    contact = context.user_data.get("new_contact", {})
    uid     = _user_id_from_context(context)

    sb   = _sb()
    row  = db.create_contact(
        sb,
        user_id=uid,
        name=contact["name"],
        company=contact["company"],
        stage=contact["stage"],
        source=source,
        team_id=_get_team_id(sb, uid),
    )

    await update.message.reply_text(
        f"✅ *Contact saved!*\n\n"
        f"👤 {contact['name']}\n"
        f"🏢 {contact['company']}\n"
        f"📊 {contact['stage']}\n"
        f"🔗 Source: {source}\n\n"
        f"Add a note? Use /addnote anytime.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

    context.user_data.pop("new_contact", None)
    return ConversationHandler.END


async def addcontact_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_contact", None)
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def build_addcontact_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addcontact", addcontact_start)],
        states={
            AC_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_name)],
            AC_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_company)],
            AC_STAGE:   [CallbackQueryHandler(addcontact_stage, pattern="^stage:")],
            AC_SOURCE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addcontact_source)],
        },
        fallbacks=[CommandHandler("cancel", addcontact_cancel)],
        allow_reentry=True,
    )


# ── /pipeline ────────────────────────────────────────────────────────────────

async def pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all active deals grouped by stage with heat score badges."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    sb      = _sb()
    grouped = db.get_contacts_by_stage(sb, uid, team_id=_get_team_id(sb, uid))

    if not grouped:
        await update.message.reply_text(
            "Your pipeline is empty.\nAdd your first contact with /addcontact"
        )
        return

    # Stage display order
    order = ["Lead", "Evaluating", "Proposal Sent", "Negotiating"]
    lines = ["📊 *Your Pipeline*\n"]

    for stage in order:
        contacts = grouped.get(stage, [])
        if not contacts:
            continue
        lines.append(f"*{stage}* ({len(contacts)})")
        for c in contacts:
            score = db.heat_score(c)
            badge = db.heat_emoji(score)
            lines.append(f"  · {c['name']} — {c['company']} · {badge} ({score})")
        lines.append("")

    # Append any stages not in the canonical order
    for stage, contacts in grouped.items():
        if stage not in order:
            lines.append(f"*{stage}* ({len(contacts)})")
            for c in contacts:
                score = db.heat_score(c)
                badge = db.heat_emoji(score)
                lines.append(f"  · {c['name']} — {c['company']} · {badge} ({score})")
            lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /context ─────────────────────────────────────────────────────────────────

async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /context [name] — Generate a pre-call brief.
    Uses the most recent matching contact's data + notes, then calls Groq.
    """
    uid = await _ensure_user(update, context)
    if not uid:
        return

    # Extract name from command args
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /context [contact name]\nExample: /context Sharma"
        )
        return

    name = " ".join(args)
    sb   = _sb()
    contact = db.get_contact_by_name(sb, uid, name, team_id=_get_team_id(sb, uid))

    if not contact:
        await update.message.reply_text(
            f"No contact found matching *{name}*.\n"
            "Check /pipeline for names or add them with /addcontact.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text("🧠 Generating pre-call brief…")

    notes  = db.get_notes_for_contact(sb, contact["id"], limit=5)
    brief  = ai.generate_pre_call_brief(contact, notes)
    score  = db.heat_score(contact)
    badge  = db.heat_emoji(score)

    await update.message.reply_text(
        f"📞 *Pre-call Brief: {contact['name']}*\n"
        f"🏢 {contact['company']} · {badge} ({score})\n\n"
        f"{brief}",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Log that a context check happened as an interaction
    db.increment_interaction(sb, contact["id"])


# ── /won & /lost ─────────────────────────────────────────────────────────────

async def won(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the most recently updated active deal as Won."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    sb      = _sb()
    contact = db.get_latest_contact(sb, uid, team_id=_get_team_id(sb, uid))

    if not contact:
        await update.message.reply_text("No active deals found. Add one with /addcontact")
        return

    db.update_contact_stage(sb, contact["id"], "Won")
    await update.message.reply_text(
        f"🎉 *Deal Won!*\n\n"
        f"Congrats on closing *{contact['name']}* from *{contact['company']}*!\n"
        f"Cha gaye aap 🚀",
        parse_mode=ParseMode.MARKDOWN,
    )


async def lost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark the most recently updated active deal as Lost."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    sb      = _sb()
    contact = db.get_latest_contact(sb, uid, team_id=_get_team_id(sb, uid))

    if not contact:
        await update.message.reply_text("No active deals found.")
        return

    db.update_contact_stage(sb, contact["id"], "Lost")
    await update.message.reply_text(
        f"📉 Marked *{contact['name']}* from *{contact['company']}* as *Lost*.\n\n"
        f"Koi baat nahi — on to the next one. /pipeline",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /addnote (2-step conversation) ───────────────────────────────────────────

async def addnote_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = await _ensure_user(update, context)
    if not uid:
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 *Add a note*\n\nWhich contact is this note for? (Type their name)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AN_CONTACT


async def addnote_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    uid  = _user_id_from_context(context)
    sb   = _sb()

    contact = db.get_contact_by_name(sb, uid, name, team_id=_get_team_id(sb, uid))
    if not contact:
        await update.message.reply_text(
            f"No contact found matching '{name}'. Try again or /cancel."
        )
        return AN_CONTACT

    context.user_data["note_contact"] = contact
    await update.message.reply_text(
        f"✅ Found: *{contact['name']}* at *{contact['company']}*\n\nNow type your note:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AN_NOTE


async def addnote_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    note_text = update.message.text.strip()
    contact   = context.user_data.get("note_contact")
    uid       = _user_id_from_context(context)
    sb        = _sb()

    db.add_note(sb, contact["id"], uid, note_text)

    await update.message.reply_text(
        f"📝 Note saved for *{contact['name']}*.\n\n_{note_text}_",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.pop("note_contact", None)
    return ConversationHandler.END


async def addnote_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("note_contact", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_addnote_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addnote", addnote_start)],
        states={
            AN_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_contact)],
            AN_NOTE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, addnote_save)],
        },
        fallbacks=[CommandHandler("cancel", addnote_cancel)],
        allow_reentry=True,
    )


# ── Forwarded message handler ─────────────────────────────────────────────────

async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Detect forwarded messages, send text to Groq for lead extraction,
    then show an inline Yes/No keyboard to confirm saving the contact.
    """
    uid = await _ensure_user(update, context)
    if not uid:
        return

    msg = update.message
    raw_text = msg.text or msg.caption or ""

    if not raw_text.strip():
        await msg.reply_text(
            "I can see you forwarded a message but it has no text. "
            "Try forwarding a text conversation."
        )
        return

    thinking = await msg.reply_text("🔍 Extracting lead from forwarded message…")

    lead = ai.extract_lead_from_message(raw_text)

    if not lead:
        await thinking.edit_text(
            "❌ Couldn't extract a lead from that message. "
            "The message may not contain contact or deal info."
        )
        return

    # Store extracted lead in context so the callback can save it
    context.user_data["pending_lead"] = {**lead, "source": "forwarded"}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Save contact", callback_data="lead_confirm_yes"),
            InlineKeyboardButton("❌ Discard", callback_data="lead_confirm_no"),
        ]
    ])

    await thinking.edit_text(
        ai.format_lead_card(lead),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def handle_lead_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline keyboard callback: save or discard the extracted lead."""
    query = update.callback_query
    await query.answer()

    uid  = _user_id_from_context(context)
    data = query.data

    if data == "lead_confirm_no":
        context.user_data.pop("pending_lead", None)
        await query.edit_message_text("Discarded. No contact saved.")
        return

    # Save to DB
    lead = context.user_data.pop("pending_lead", None)
    if not lead or not uid:
        await query.edit_message_text("Session expired. Please forward the message again.")
        return

    sb           = _sb()
    contact_name = lead.get("contact_name", "Unknown")
    stage        = lead.get("stage", "Lead")
    team_id      = _get_team_id(sb, uid)

    # ── Deduplication: check for existing contact with same name ──────────────
    existing = db.get_contact_by_name(sb, uid, contact_name, team_id=team_id)

    if existing:
        row = existing
        # Update stage if the new one is further along
        db.update_contact_stage(sb, row["id"], stage)
        result_msg = (
            f"🔄 *{contact_name}* already in pipeline — updated stage to *{stage}*.\n\n"
            f"Use /pipeline to view."
        )
    else:
        row = db.create_contact(
            sb,
            user_id=uid,
            name=contact_name,
            company=lead.get("company", "Unknown"),
            stage=stage,
            source=lead.get("source", "forwarded"),
            team_id=team_id,
        )
        result_msg = (
            f"✅ *{contact_name}* from *{lead.get('company')}* saved to pipeline!\n\n"
            f"Stage: {stage} · Use /pipeline to view."
        )

    # Log topic and next action as notes either way
    if row and lead.get("topic"):
        db.add_note(sb, row["id"], uid, f"Topic: {lead['topic']}")
    if row and lead.get("next_action"):
        db.add_note(sb, row["id"], uid, f"Next action: {lead['next_action']}")

    await query.edit_message_text(result_msg, parse_mode=ParseMode.MARKDOWN)


# ── Plain text handler (pasted notes / non-forwarded messages) ───────────────

async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle plain text messages (not commands, not forwarded).
    Sends text to Groq for lead extraction, then shows a Yes/No inline keyboard.
    """
    uid = await _ensure_user(update, context)
    if not uid:
        return

    raw_text = update.message.text.strip()
    if not raw_text:
        return

    thinking = await update.message.reply_text("🔍 Extracting lead from message…")

    lead = ai.extract_lead_from_message(raw_text)

    if not lead:
        await thinking.edit_text(
            "❌ Couldn't extract a lead from that message. "
            "Try /addcontact to log a contact manually."
        )
        return

    context.user_data["pending_lead"] = {**lead, "source": "manual"}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Save contact", callback_data="lead_confirm_yes"),
            InlineKeyboardButton("❌ Discard", callback_data="lead_confirm_no"),
        ]
    ])

    await thinking.edit_text(
        ai.format_lead_card(lead),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ── Voice note handler ────────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Receive a voice note or audio file.
    Downloads → Whisper transcription → Groq lead extraction → Yes/No inline keyboard.
    """
    uid = await _ensure_user(update, context)
    if not uid:
        return

    msg   = update.message
    voice = msg.voice or msg.audio
    if not voice:
        return

    # ── Size guard (Whisper hard limit: 25 MB) ────────────────────────────────
    file_size    = voice.file_size or 0
    duration_sec = getattr(voice, "duration", 0) or 0

    if file_size > 25 * 1024 * 1024:
        await msg.reply_text(
            "⚠️ Voice note too large. Please send audio under 25 MB."
        )
        return

    # Determine MIME type (PTB sets mime_type on Audio; Voice is always OGG)
    mime_type = getattr(voice, "mime_type", None) or "audio/ogg"

    status = await msg.reply_text("🎙 Transcribing voice note…")

    # ── Download ──────────────────────────────────────────────────────────────
    try:
        tg_file     = await context.bot.get_file(voice.file_id)
        audio_bytes = await tg_file.download_as_bytearray()
    except Exception as e:
        logger.error("Failed to download voice file: %s", e)
        await status.edit_text(
            "❌ Voice transcription failed — couldn't download the file. "
            "Please type the update instead."
        )
        return

    # ── Transcribe ────────────────────────────────────────────────────────────
    try:
        transcript = await ai.transcribe_voice(
            bytes(audio_bytes), duration_sec, mime_type
        )
    except ValueError as e:
        if str(e) == "audio_too_large":
            await status.edit_text(
                "⚠️ Voice note too large. Please send audio under 25 MB."
            )
        else:  # empty_transcript
            await status.edit_text(
                "❌ Couldn't understand the audio. Please try again or type the update instead."
            )
        return
    except RuntimeError:
        await status.edit_text(
            "❌ Voice transcription failed. Please type the update instead."
        )
        return

    # ── Show transcript + extract lead ────────────────────────────────────────
    await status.edit_text(
        f"🎙 *Transcript:*\n_{transcript}_\n\n🔍 Extracting lead…",
        parse_mode=ParseMode.MARKDOWN,
    )

    lead = ai.extract_lead_from_message(transcript)

    if not lead:
        await status.edit_text(
            f"🎙 *Transcript:*\n_{transcript}_\n\n"
            "❌ Couldn't extract a lead. Use /addcontact to log manually.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Store for callback
    context.user_data["pending_lead"] = {**lead, "source": "voice"}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Save contact", callback_data="lead_confirm_yes"),
            InlineKeyboardButton("❌ Discard",      callback_data="lead_confirm_no"),
        ]
    ])

    await status.edit_text(
        f"🎙 *Transcript:*\n_{transcript}_\n\n" + ai.format_lead_card(lead),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ── /digest ──────────────────────────────────────────────────────────────────

async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the daily digest for the calling user."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    await update.message.reply_text("☀️ Generating your pipeline digest…")
    sb   = _sb()
    user = db.get_user(sb, update.effective_chat.id)
    await sched.send_digest_for_user(context.bot, user)


# ── /nudge ────────────────────────────────────────────────────────────────────

async def nudge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger inactivity nudge check for the calling user."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    await update.message.reply_text("🔍 Checking for overdue contacts…")
    sb   = _sb()
    user = db.get_user(sb, update.effective_chat.id)
    await sched.send_nudges_for_user(context.bot, user)


# ── /ask ─────────────────────────────────────────────────────────────────────

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ask [question] — Answer a natural language question about the pipeline.
    Fetches live data from Supabase and sends it to Groq for analysis.
    """
    uid = await _ensure_user(update, context)
    if not uid:
        return

    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text(
            "Usage: /ask [your question]\n\n"
            "Examples:\n"
            "• /ask who are my hottest leads?\n"
            "• /ask which deals haven't been touched in 2 weeks?\n"
            "• /ask what should I focus on today?"
        )
        return

    thinking = await update.message.reply_text("🤔 Analysing your pipeline…")

    sb       = _sb()
    contacts = db.get_active_contacts(sb, uid, team_id=_get_team_id(sb, uid))
    notes    = db.get_recent_notes_for_user(sb, uid, limit=40)

    answer = ai.answer_pipeline_question(question, contacts, notes)

    await thinking.edit_text(
        f"💬 *{question}*\n\n{answer}",
        parse_mode=None,
    )


# ── /createteam ──────────────────────────────────────────────────────────────

async def createteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/createteam [name] — Create a new shared team pipeline."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    name = " ".join(context.args or []).strip() or "My Team"
    sb   = _sb()

    # Block if already in a team
    existing = db.get_user_team(sb, uid)
    if existing:
        await update.message.reply_text(
            f"⚠️ You're already in team *{existing['name']}*.\n"
            f"Invite code: `{existing['invite_code']}`\n\n"
            f"Use /myteam to see members.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    team = db.create_team(sb, uid, name)
    if not team:
        await update.message.reply_text("❌ Failed to create team. Please try again.")
        return

    await update.message.reply_text(
        f"🎉 Team *{team['name']}* created!\n\n"
        f"Share this invite code with your teammates:\n"
        f"`{team['invite_code']}`\n\n"
        f"They can join with: /jointeam {team['invite_code']}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /jointeam ─────────────────────────────────────────────────────────────────

async def jointeam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/jointeam <code> — Join a team using an invite code."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    if not context.args:
        await update.message.reply_text("Usage: /jointeam <invite-code>\nExample: /jointeam ABC123")
        return

    code = context.args[0].strip()
    sb   = _sb()
    team, err = db.join_team(sb, uid, code)

    if err == "invalid_code":
        await update.message.reply_text("❌ Invalid invite code. Check the code and try again.")
    elif err == "already_member":
        await update.message.reply_text(
            f"✅ You're already in team *{team['name']}*. Use /myteam to see members.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif err == "already_in_team":
        await update.message.reply_text(
            "⚠️ You're already in another team. You can only be in one team at a time."
        )
    else:
        await update.message.reply_text(
            f"✅ Joined team *{team['name']}*!\n\n"
            f"You can now see your team's shared pipeline with /pipeline.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /myteam ───────────────────────────────────────────────────────────────────

async def myteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myteam — Show current team and its members."""
    uid = await _ensure_user(update, context)
    if not uid:
        return

    sb   = _sb()
    team = db.get_user_team(sb, uid)

    if not team:
        await update.message.reply_text(
            "You're not part of a team yet.\n"
            "• Create one: /createteam [name]\n"
            "• Join one: /jointeam <code>"
        )
        return

    members = db.get_team_members(sb, team["id"])
    lines   = [f"👥 *Team: {team['name']}*", f"Invite code: `{team['invite_code']}`\n"]

    for m in members:
        role_badge = "👑" if m["role"] == "owner" else "👤"
        joined     = str(m.get("joined_at", ""))[:10]
        lines.append(f"{role_badge} {m['user_name']} — {m['role']} (since {joined})")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Unknown command fallback ──────────────────────────────────────────────────

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hmm, I don't know that command. Type /help to see what I can do."
    )
