"""
ai.py — Groq-powered AI extraction + OpenAI Whisper transcription for Unnati CRM.
"""

import io
import os
import json
import logging
from groq import Groq
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

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


def get_groq_client() -> Groq:
    """Create a Groq client from env var."""
    return Groq(api_key=os.environ["GROQ_API_KEY"])


def extract_lead_from_message(raw_text: str) -> dict | None:
    """
    Send `raw_text` to Groq and parse the returned JSON lead card.
    Returns a dict with keys: contact_name, company, stage, topic, next_action, confidence.
    Returns None on any failure.
    """
    client = get_groq_client()

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",   # fast, free-tier friendly
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user",   "content": f"Extract lead from this message:\n\n{raw_text}"},
            ],
            temperature=0.1,          # low temp = consistent structured output
            max_tokens=300,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        print(f"[DEBUG] Raw Groq response:\n{content}\n")

        data = json.loads(content)

        # Validate stage value, fall back to Lead
        if data.get("stage") not in VALID_STAGES:
            data["stage"] = "Lead"

        return data

    except json.JSONDecodeError as e:
        logger.error("Groq returned non-JSON: %s", e)
        print(f"[DEBUG] JSON parse error: {e}")
        print(f"[DEBUG] Raw content that failed to parse: {content!r}")
        return None
    except Exception as e:
        logger.error("Groq extraction failed: %s", e)
        print(f"[DEBUG] Groq exception ({type(e).__name__}): {e}")
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
    Falls back to a static template if Groq is unavailable.
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

    client = get_groq_client()
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": CONTEXT_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.4,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Pre-call brief generation failed: %s", e)
        # Graceful fallback — always give the founder something useful
        return (
            f"• {contact.get('name')} from {contact.get('company')} · Stage: {contact.get('stage')}\n"
            f"• {contact.get('interaction_count', 0)} interactions logged\n"
            f"• Last note: {notes[0].get('note_text', 'None') if notes else 'None'}"
        )


# ── Whisper transcription ─────────────────────────────────────────────────────

WHISPER_COST_PER_MINUTE = 0.006  # USD, as of 2024

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — Whisper API hard limit


async def transcribe_voice(audio_bytes: bytes, duration_seconds: int, mime_type: str = "audio/ogg") -> str:
    """
    Transcribe audio bytes using OpenAI Whisper API.

    Args:
        audio_bytes:      Raw audio data.
        duration_seconds: Clip duration (for cost logging).
        mime_type:        MIME type hint — determines filename extension sent to Whisper.

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
    cost_usd = (duration_seconds / 60) * WHISPER_COST_PER_MINUTE
    print(
        f"[WHISPER] Sending {len(audio_bytes) / 1024:.1f} KB "
        f"({duration_seconds}s) to Whisper. "
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
