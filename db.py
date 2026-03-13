"""
db.py — All Supabase database operations for Unnati CRM.
Uses raw httpx calls to the Supabase REST API (PostgREST) instead of
the supabase Python package, avoiding dependency conflicts on Python 3.14.
Every function takes an httpx.Client as first arg so callers control the connection.
"""

import os
import httpx
from datetime import datetime, timezone


# ── Client factory ──────────────────────────────────────────────────────────

def get_client() -> httpx.Client:
    """Create and return an httpx client pre-configured for Supabase REST API."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return httpx.Client(
        base_url=f"{url}/rest/v1/",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        timeout=10.0,
    )


def _data(r: httpx.Response) -> list:
    r.raise_for_status()
    result = r.json()
    return result if isinstance(result, list) else [result]


# ── Users ────────────────────────────────────────────────────────────────────

def upsert_user(sb: httpx.Client, chat_id: int, name: str) -> dict:
    """Insert a new user or return existing one. Telegram chat_id is the unique identifier."""
    r = sb.post(
        "users",
        json={"telegram_chat_id": chat_id, "name": name},
        headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    data = _data(r)
    return data[0] if data else {}


# Alias used by callers that expect create_user
create_user = upsert_user


def get_user(sb: httpx.Client, chat_id: int) -> dict | None:
    """Fetch user row by telegram chat_id. Returns None if not found."""
    r = sb.get("users", params={"telegram_chat_id": f"eq.{chat_id}", "limit": "1"})
    data = _data(r)
    return data[0] if data else None


# ── Contacts ─────────────────────────────────────────────────────────────────

def create_contact(
    sb: httpx.Client,
    user_id: str,
    name: str,
    company: str,
    stage: str,
    source: str = "manual",
) -> dict:
    """Insert a new contact and return the created row."""
    now = datetime.now(timezone.utc).isoformat()
    r = sb.post(
        "contacts",
        json={
            "user_id": user_id,
            "name": name,
            "company": company,
            "stage": stage,
            "source": source,
            "added_on": now,
            "last_updated": now,
            "interaction_count": 1,
        },
        headers={"Prefer": "return=representation"},
    )
    data = _data(r)
    return data[0] if data else {}


def get_contacts_by_stage(sb: httpx.Client, user_id: str) -> dict[str, list]:
    """
    Fetch all non-terminal contacts for a user, grouped by stage.
    Returns a dict: { stage_name: [contact, ...] }
    """
    r = sb.get(
        "contacts",
        params={
            "user_id": f"eq.{user_id}",
            "stage": "not.in.(Won,Lost)",
            "order": "last_updated.desc",
        },
    )
    grouped: dict[str, list] = {}
    for row in _data(r):
        grouped.setdefault(row["stage"], []).append(row)
    return grouped


# Alias used by callers that expect get_contacts
get_contacts = get_contacts_by_stage


def get_contact_by_name(sb: httpx.Client, user_id: str, name: str) -> dict | None:
    """Find the most recently updated contact whose name contains `name` (case-insensitive)."""
    r = sb.get(
        "contacts",
        params={
            "user_id": f"eq.{user_id}",
            "name": f"ilike.%{name}%",
            "order": "last_updated.desc",
            "limit": "1",
        },
    )
    data = _data(r)
    return data[0] if data else None


def get_all_users(sb: httpx.Client) -> list:
    """Return every registered user (for scheduler broadcast)."""
    r = sb.get("users")
    return _data(r)


def get_active_contacts(sb: httpx.Client, user_id: str) -> list:
    """Return a flat list of all non-terminal contacts for a user."""
    r = sb.get(
        "contacts",
        params={
            "user_id": f"eq.{user_id}",
            "stage": "not.in.(Won,Lost)",
            "order": "last_updated.asc",
        },
    )
    return _data(r)


def get_latest_contact(sb: httpx.Client, user_id: str) -> dict | None:
    """Return the contact that was most recently touched."""
    r = sb.get(
        "contacts",
        params={
            "user_id": f"eq.{user_id}",
            "stage": "not.in.(Won,Lost)",
            "order": "last_updated.desc",
            "limit": "1",
        },
    )
    data = _data(r)
    return data[0] if data else None


def update_contact_stage(sb: httpx.Client, contact_id: str, stage: str) -> dict:
    """Update the stage and bump last_updated timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    r = sb.patch(
        "contacts",
        json={"stage": stage, "last_updated": now},
        params={"id": f"eq.{contact_id}"},
        headers={"Prefer": "return=representation"},
    )
    data = _data(r)
    return data[0] if data else {}


def increment_interaction(sb: httpx.Client, contact_id: str) -> None:
    """Bump interaction_count and refresh last_updated for heat score calculation."""
    now = datetime.now(timezone.utc).isoformat()
    r = sb.get("contacts", params={"id": f"eq.{contact_id}", "select": "interaction_count", "limit": "1"})
    rows = _data(r)
    current = (rows[0] if rows else {}).get("interaction_count", 0)
    sb.patch(
        "contacts",
        json={"interaction_count": current + 1, "last_updated": now},
        params={"id": f"eq.{contact_id}"},
        headers={"Prefer": "return=representation"},
    ).raise_for_status()


# ── Notes ────────────────────────────────────────────────────────────────────

def add_note(sb: httpx.Client, contact_id: str, user_id: str, text: str) -> dict:
    """Append a note and also bump the contact's interaction count."""
    now = datetime.now(timezone.utc).isoformat()
    r = sb.post(
        "notes",
        json={
            "contact_id": contact_id,
            "user_id": user_id,
            "note_text": text,
            "logged_on": now,
        },
        headers={"Prefer": "return=representation"},
    )
    data = _data(r)
    increment_interaction(sb, contact_id)
    return data[0] if data else {}


# Alias used by callers that expect create_note
create_note = add_note


def get_notes_for_contact(sb: httpx.Client, contact_id: str, limit: int = 5) -> list:
    """Return the N most recent notes for a contact."""
    r = sb.get(
        "notes",
        params={
            "contact_id": f"eq.{contact_id}",
            "order": "logged_on.desc",
            "limit": str(limit),
        },
    )
    return _data(r)


def get_recent_notes_for_user(sb: httpx.Client, user_id: str, limit: int = 40) -> list:
    """Return the most recent notes across all of a user's contacts."""
    r = sb.get(
        "notes",
        params={
            "user_id": f"eq.{user_id}",
            "order": "logged_on.desc",
            "limit": str(limit),
        },
    )
    return _data(r)


# ── Heat score ───────────────────────────────────────────────────────────────

def heat_score(contact: dict) -> int:
    """
    Calculate deal heat score at read-time (no DB column needed).
    Formula: 100 - (days_since_last_update * 5) + (interaction_count * 3), capped 0-100.
    """
    last_updated_str = contact.get("last_updated") or contact.get("added_on")
    interaction_count = contact.get("interaction_count", 0)

    if last_updated_str:
        lu = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - lu).days
    else:
        days = 30  # assume cold if no data

    score = 100 - (days * 5) + (interaction_count * 3)
    return max(0, min(100, score))


def heat_emoji(score: int) -> str:
    """Return flame emoji tier based on score."""
    if score >= 70:
        return "🔥 Hot"
    if score >= 40:
        return "🌤 Warm"
    return "🧊 Cold"
