"""
db.py — All Supabase database operations for Unnati CRM.
Uses raw httpx calls to the Supabase REST API (PostgREST) instead of
the supabase Python package, avoiding dependency conflicts on Python 3.14.
Every function takes an httpx.Client as first arg so callers control the connection.
"""

import os
import random
import string
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

def _contact_filter(user_id: str, team_id: str | None) -> dict:
    """Return the right ownership filter for contact queries.
    When in a team, show contacts tagged with team_id OR owned by user_id (backwards compat
    for contacts created before team membership). Solo users see only their own."""
    if team_id:
        return {"or": f"(team_id.eq.{team_id},user_id.eq.{user_id})"}
    return {"user_id": f"eq.{user_id}"}


def create_contact(
    sb: httpx.Client,
    user_id: str,
    name: str,
    company: str,
    stage: str,
    source: str = "manual",
    team_id: str | None = None,
) -> dict:
    """Insert a new contact and return the created row."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "user_id": user_id,
        "name": name,
        "company": company,
        "stage": stage,
        "source": source,
        "added_on": now,
        "last_updated": now,
        "interaction_count": 1,
    }
    if team_id:
        payload["team_id"] = team_id
    r = sb.post(
        "contacts",
        json=payload,
        headers={"Prefer": "return=representation"},
    )
    data = _data(r)
    return data[0] if data else {}


def get_contacts_by_stage(
    sb: httpx.Client, user_id: str, team_id: str | None = None
) -> dict[str, list]:
    """
    Fetch all non-terminal contacts for a user (or their team), grouped by stage.
    Returns a dict: { stage_name: [contact, ...] }
    """
    r = sb.get(
        "contacts",
        params={
            **_contact_filter(user_id, team_id),
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


def get_contact_by_name(
    sb: httpx.Client, user_id: str, name: str, team_id: str | None = None
) -> dict | None:
    """Find the most recently updated contact whose name contains `name` (case-insensitive)."""
    r = sb.get(
        "contacts",
        params={
            **_contact_filter(user_id, team_id),
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


def get_active_contacts(
    sb: httpx.Client, user_id: str, team_id: str | None = None
) -> list:
    """Return a flat list of all non-terminal contacts for a user (or their team)."""
    r = sb.get(
        "contacts",
        params={
            **_contact_filter(user_id, team_id),
            "stage": "not.in.(Won,Lost)",
            "order": "last_updated.asc",
        },
    )
    return _data(r)


def get_latest_contact(
    sb: httpx.Client, user_id: str, team_id: str | None = None
) -> dict | None:
    """Return the contact that was most recently touched."""
    r = sb.get(
        "contacts",
        params={
            **_contact_filter(user_id, team_id),
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


# ── Teams ────────────────────────────────────────────────────────────────────

def _random_invite_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _resolve_chat_id(sb: httpx.Client, user_uuid: str) -> int | None:
    """Look up the Telegram chat_id (bigint) for a Supabase user UUID."""
    r = sb.get("users", params={"id": f"eq.{user_uuid}", "select": "telegram_chat_id", "limit": "1"})
    rows = _data(r)
    return rows[0]["telegram_chat_id"] if rows else None


def create_team(sb: httpx.Client, user_id: str, name: str) -> dict | None:
    """Create a new team, add creator as owner, return team dict. Returns None on failure."""
    chat_id = _resolve_chat_id(sb, user_id)
    if chat_id is None:
        return None
    for _ in range(5):  # retry on rare invite_code collision
        code = _random_invite_code()
        r = sb.post(
            "teams",
            json={"name": name, "created_by": chat_id, "invite_code": code},
            headers={"Prefer": "return=representation"},
        )
        if r.status_code not in (200, 201):
            continue
        team = _data(r)[0]
        sb.post(
            "team_members",
            json={"team_id": team["id"], "user_id": chat_id, "role": "owner"},
            headers={"Prefer": "return=representation"},
        ).raise_for_status()
        return team
    return None


def get_user_team(sb: httpx.Client, user_id: str) -> dict | None:
    """Return the team the user belongs to, or None if solo or on any error."""
    try:
        chat_id = _resolve_chat_id(sb, user_id)
        if chat_id is None:
            return None
        r = sb.get("team_members", params={"user_id": f"eq.{chat_id}", "limit": "1"})
        rows = _data(r)
        if not rows:
            return None
        r2 = sb.get("teams", params={"id": f"eq.{rows[0]['team_id']}", "limit": "1"})
        d2 = _data(r2)
        return d2[0] if d2 else None
    except Exception:
        return None


def join_team(
    sb: httpx.Client, user_id: str, invite_code: str
) -> tuple[dict | None, str | None]:
    """Join a team by invite code. Returns (team, error_key) — error_key is None on success."""
    chat_id = _resolve_chat_id(sb, user_id)
    if chat_id is None:
        return None, "invalid_code"
    r = sb.get("teams", params={"invite_code": f"eq.{invite_code.upper()}", "limit": "1"})
    teams = _data(r)
    if not teams:
        return None, "invalid_code"
    team = teams[0]
    # Already a member of this or any team?
    r2 = sb.get("team_members", params={"user_id": f"eq.{chat_id}", "limit": "1"})
    existing = _data(r2)
    if existing:
        if existing[0]["team_id"] == team["id"]:
            return team, "already_member"
        return None, "already_in_team"
    sb.post(
        "team_members",
        json={"team_id": team["id"], "user_id": chat_id, "role": "member"},
        headers={"Prefer": "return=representation"},
    ).raise_for_status()
    return team, None


def get_team_members(sb: httpx.Client, team_id: str) -> list:
    """Return all members of a team, each with user name and role."""
    r = sb.get("team_members", params={"team_id": f"eq.{team_id}", "select": "role,joined_at,user_id"})
    members = _data(r)
    result = []
    for m in members:
        # team_members.user_id is telegram_chat_id (bigint) — look up by that
        ur = sb.get("users", params={"telegram_chat_id": f"eq.{m['user_id']}", "select": "name", "limit": "1"})
        ud = _data(ur)
        result.append({**m, "user_name": ud[0]["name"] if ud else "Unknown"})
    return result


# ── Reset ────────────────────────────────────────────────────────────────────

def delete_user_contacts(sb: httpx.Client, user_id: str, team_id: str | None = None) -> int:
    """Delete all contacts for a user (or their team). Returns count deleted."""
    params = {**_contact_filter(user_id, team_id)}
    r = sb.delete(
        "contacts",
        params=params,
        headers={"Prefer": "return=representation"},
    )
    data = r.json() if r.status_code in (200, 201) else []
    return len(data) if isinstance(data, list) else 0


# ── Bot ↔ Web Link Tokens ────────────────────────────────────────────────────

def consume_link_token(sb: httpx.Client, token: str, bot_user_id: str) -> bool:
    """Validate a one-time link token and attach supabase_auth_id to the bot user row.
    Returns True on success, False if token is expired, used, or not found."""
    r = sb.get(
        "bot_link_tokens",
        params={
            "token": f"eq.{token}",
            "used": "eq.false",
            "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}",
            "limit": "1",
        },
    )
    rows = _data(r)
    if not rows:
        return False
    auth_id = rows[0]["supabase_auth_id"]
    # Link the web auth UUID to this bot user
    sb.patch(
        "users",
        json={"supabase_auth_id": auth_id},
        params={"id": f"eq.{bot_user_id}"},
        headers={"Prefer": "return=representation"},
    ).raise_for_status()
    # Mark token used
    sb.patch(
        "bot_link_tokens",
        json={"used": True},
        params={"token": f"eq.{token}"},
    ).raise_for_status()
    return True


# ── Reminders ─────────────────────────────────────────────────────────────────

def create_reminder(sb: httpx.Client, chat_id: int, remind_at: datetime, message: str) -> dict:
    """Insert a new reminder row and return it."""
    r = sb.post(
        "reminders",
        json={
            "chat_id": chat_id,
            "remind_at": remind_at.isoformat(),
            "message": message,
        },
        headers={"Prefer": "return=representation"},
    )
    data = _data(r)
    return data[0] if data else {}


def get_due_reminders(sb: httpx.Client) -> list:
    """Return all unsent reminders whose remind_at is in the past."""
    r = sb.get(
        "reminders",
        params={
            "sent": "eq.false",
            "remind_at": f"lt.{datetime.now(timezone.utc).isoformat()}",
        },
    )
    return _data(r)


def mark_reminder_sent(sb: httpx.Client, reminder_id: str) -> None:
    """Mark a reminder as sent."""
    sb.patch(
        "reminders",
        json={"sent": True},
        params={"id": f"eq.{reminder_id}"},
    ).raise_for_status()


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
