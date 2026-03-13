"""
ai.py — Anthropic Claude-powered AI extraction + OpenAI Whisper transcription for Unnati CRM.
"""

import io
import os
import json
import logging
import anthropic
from groq import Groq
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

# Valid stage values the AI must choose from
VALID_STAGES = ["Lead", "Evaluating", "Proposal Sent", "Negotiating", "Won", "Lost"]

# System prompt for the lead extraction task
EXTRACTION_SYSTEM_PROMPT = """
You are a CRM data extractor for an Indian B2B sales assistant called Unnati CRM.

The user will paste a forwarded message or conversation snippet.
Your job is to extract a structured lead record from it.

Respond ONLY with a valid JSON object — no explanation, no markdown, no code fences.

JSON schema (all fields required):
{
  "contact_name": "string — full name of the prospect or their key contact",
  "company":      "string — company/org name (use 'Unknown' if not found)",
  "stage":        "one of: Lead | Evaluating | Proposal Sent | Negotiating | Won | Lost",
  "topic":        "string — what the conversation is about (product, service, use-case)",
  "next_action":  "string — concrete next step the founder should take",
  "sentiment":    "positive | neutral | negative — tone of the conversation",
  "confidence":   "high | medium | low — how sure you are about the extraction"
}

Rules:
- If a field is genuinely missing, use a sensible default (e.g. stage = "Lead").
- Names in Hinglish or abbreviated form are fine — keep them as-is.
- next_action should be actionable: "Call back Thursday 4pm" beats "Follow up".
- Never invent data that isn't implied by the message.
"""


def get_anthropic_client() -> anthropic.Anthropic:
    """Create an Anthropic client from env var."""
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def extract_lead_from_message(raw_text: str) -> dict | None:
    """
    Send `raw_text` to Claude and parse the returned JSON lead card.
    Returns a dict with keys: contact_name, company, stage, topic, next_action, confidence.
    Returns None on any failure.
    """
    client = get_anthropic_client()

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"Extract lead from this message:\n\n{raw_text}"},
            ],
        )

        content = response.content[0].text
        print(f"[DEBUG] Raw Claude response:\n{content}\n")

        data = json.loads(content)

        # Validate stage value, fall back to Lead
        if data.get("stage") not in VALID_STAGES:
            data["stage"] = "Lead"

        return data

    except json.JSONDecodeError as e:
        logger.error("Claude returned non-JSON: %s", e)
        print(f"[DEBUG] JSON parse error: {e}")
        print(f"[DEBUG] Raw content that failed to parse: {content!r}")
        return None
    except Exception as e:
        logger.error("Claude extraction failed: %s", e)
        print(f"[DEBUG] Claude exception ({type(e).__name__}): {e}")
        return None


def format_lead_card(lead: dict) -> str:
    """
    Format a parsed lead dict into a readable Telegram message card.
    """
    confidence_emoji = {"high": "✅", "medium": "🟡", "low": "⚠️"}.get(
        lead.get("confidence", "low"), "❓"
    )

    sentiment_emoji = {"positive": "😊", "neutral": "😐", "negative": "😟"}.get(
        lead.get("sentiment", "neutral"), "😐"
    )

    return (
        f"📋 *Lead Detected*\n\n"
        f"👤 *Name:* {lead.get('contact_name', '—')}\n"
        f"🏢 *Company:* {lead.get('company', '—')}\n"
        f"📊 *Stage:* {lead.get('stage', 'Lead')}\n"
        f"💬 *Topic:* {lead.get('topic', '—')}\n"
        f"➡️ *Next action:* {lead.get('next_action', '—')}\n"
        f"{sentiment_emoji} *Sentiment:* {lead.get('sentiment', '—').capitalize()}\n"
        f"{confidence_emoji} *Confidence:* {lead.get('confidence', '—').capitalize()}\n\n"
        f"Save this contact to your pipeline?"
    )


# ── Pre-call context generation ──────────────────────────────────────────────

CONTEXT_SYSTEM_PROMPT = """
You are a pre-call coach for an Indian B2B founder.
Given a contact's CRM data and recent notes, write a sharp 3-bullet pre-call brief.

Format:
• What we know about them
• Where they are in the buying journey
• Recommended talking points / questions for this call

Keep it under 120 words. Punchy. No fluff. Respond in plain text (no markdown headers).
"""


def generate_pre_call_brief(contact: dict, notes: list[dict]) -> str:
    """
    Generate a pre-call context brief for a contact using their CRM data + notes.
    Falls back to a static template if Claude is unavailable.
    """
    notes_text = "\n".join(
        f"- [{n.get('logged_on','')[:10]}] {n.get('note_text','')}"
        for n in notes
    ) or "No notes logged yet."

    prompt = (
        f"Contact: {contact.get('name')} at {contact.get('company')}\n"
        f"Stage: {contact.get('stage')}\n"
        f"Source: {contact.get('source')}\n"
        f"Interactions: {contact.get('interaction_count', 0)}\n\n"
        f"Recent notes:\n{notes_text}"
    )

    client = get_anthropic_client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=CONTEXT_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": prompt},
            ],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error("Pre-call brief generation failed: %s", e)
        # Graceful fallback — always give the founder something useful
        return (
            f"• {contact.get('name')} from {contact.get('company')} · Stage: {contact.get('stage')}\n"
            f"• {contact.get('interaction_count', 0)} interactions logged\n"
            f"• Last note: {notes[0].get('note_text', 'None') if notes else 'None'}"
        )


# ── Pipeline Q&A ─────────────────────────────────────────────────────────────

PIPELINE_QA_SYSTEM_PROMPT = """
You are Unnati CRM's AI assistant for an Indian B2B founder.
You have been given the founder's live pipeline data — all their active contacts
with deal stage, heat score, days since last update, interaction count, and
recent notes.

Answer the founder's question conversationally but concisely.
Use the data provided; do not make up contacts or facts not in the data.
Format lists as bullet points. Keep your answer under 200 words.
If the data doesn't support a clear answer, say so honestly.
"""


def answer_pipeline_question(question: str, contacts: list, notes: list) -> str:
    """
    Answer a natural language question about the user's pipeline.

    Args:
        question:  The founder's free-text question.
        contacts:  List of active contact dicts from db.get_active_contacts().
        notes:     List of recent note dicts from db.get_recent_notes_for_user().

    Returns:
        Plain-text answer string, or an error message.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # Build a compact notes index: contact_id → [note_text, ...]
    notes_by_contact: dict[str, list[str]] = {}
    for n in notes:
        cid = n.get("contact_id", "")
        notes_by_contact.setdefault(cid, []).append(
            f"[{str(n.get('logged_on',''))[:10]}] {n.get('note_text','')}"
        )

    # Serialize each contact into a readable block
    contact_lines: list[str] = []
    for c in contacts:
        lu_str = c.get("last_updated") or c.get("added_on", "")
        try:
            lu = datetime.fromisoformat(lu_str.replace("Z", "+00:00"))
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            days_ago = (now - lu).days
        except Exception:
            days_ago = "?"

        # Import here to avoid circular; db is always available at runtime
        import db as _db
        score = _db.heat_score(c)
        badge = _db.heat_emoji(score)

        recent_notes = notes_by_contact.get(c.get("id", ""), [])
        notes_str = "; ".join(recent_notes[:3]) if recent_notes else "no notes"

        contact_lines.append(
            f"- {c['name']} ({c['company']}) | Stage: {c['stage']} | "
            f"{badge} score={score} | last touched {days_ago}d ago | "
            f"interactions: {c.get('interaction_count', 0)} | notes: {notes_str}"
        )

    if not contact_lines:
        return "Your pipeline is empty — add contacts with /addcontact first."

    pipeline_context = "PIPELINE DATA:\n" + "\n".join(contact_lines)
    user_prompt = f"{pipeline_context}\n\nQUESTION: {question}"

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=400,
            messages=[
                {"role": "system", "content": PIPELINE_QA_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Pipeline Q&A failed: %s", e, exc_info=True)
        print(f"[DEBUG] Pipeline Q&A error ({type(e).__name__}): {e}")
        import traceback; traceback.print_exc()
        return "Sorry, I couldn't process that question right now. Try again in a moment."


# ── Whisper transcription ─────────────────────────────────────────────────────

WHISPER_COST_PER_MINUTE = 0.006  # USD, as of 2024

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — Whisper API hard limit


async def transcribe_voice(audio_bytes: bytes, duration_sec: int, mime_type: str = "audio/ogg") -> str:
    """
    Transcribe audio bytes using OpenAI Whisper API.

    Args:
        audio_bytes:  Raw audio data.
        duration_sec: Clip duration in seconds (for cost logging).
        mime_type:    MIME type hint — determines filename extension sent to Whisper.

    Returns:
        Transcribed text string.

    Raises:
        ValueError:  If audio exceeds 25 MB or transcription is empty.
        RuntimeError: On API errors.
    """
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("audio_too_large")

    # Map MIME type → file extension Whisper recognises
    ext_map = {
        "audio/ogg":  "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4":  "mp4",
        "audio/m4a":  "m4a",
        "audio/wav":  "wav",
        "audio/webm": "webm",
    }
    ext = ext_map.get(mime_type, "ogg")

    # Build a file-like object with a name so the SDK detects the format
    buf = io.BytesIO(audio_bytes)
    buf.name = f"audio.{ext}"

    # Log estimated cost before the API call
    cost_usd = (duration_sec / 60) * WHISPER_COST_PER_MINUTE
    print(
        f"[WHISPER] Sending {len(audio_bytes) / 1024:.1f} KB "
        f"({duration_sec}s) to Whisper. "
        f"Est. cost: ${cost_usd:.4f}"
    )

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    try:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="en",        # speeds up inference; Whisper auto-detects if omitted
            response_format="text",
        )
    except Exception as e:
        logger.error("Whisper API error: %s", e)
        print(f"[WHISPER] API error ({type(e).__name__}): {e}")
        raise RuntimeError("whisper_api_error") from e

    text = result.strip() if isinstance(result, str) else str(result).strip()
    print(f"[WHISPER] Transcript: {text!r}")

    if not text:
        raise ValueError("empty_transcript")

    return text
