"""Application service — Phase 3.4.

Handles the DB operations for self-service citizen applications:
submitting, listing, approving, and rejecting. The cog (cogs/applications.py)
handles Discord interaction; this module is the data layer.

Applications flow:
    /apply ign settlement recruiter → status=pending → posted to APPLICATIONS_CHANNEL_ID
    Council clicks Approve → citizen_add path runs → status=approved
    Council clicks Reject → status=rejected
"""

import logging

from core import database as db

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


async def submit_application(
    ign: str,
    applicant_discord_id: str,
    settlement: str,
    recruiter_discord_id: str | None = None,
) -> dict | None:
    """Submit a new application. Returns the created row, or None on conflict.

    The partial unique index (uq_applications_pending_per_user) prevents a user
    from having two pending applications at once — the IntegrityError is caught
    and surfaced as None so the cog can show a clear error.
    """
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO citizen_applications "
                "(ign, applicant_discord_id, settlement, recruiter_discord_id, status) "
                "VALUES ($1, $2, $3, $4, 'pending') RETURNING *",
                ign,
                applicant_discord_id,
                settlement,
                recruiter_discord_id,
            )
            return dict(row) if row else None
        except Exception as e:
            logger.warning(f"Application submission failed (likely duplicate pending): {e}")
            return None


async def get_pending_applications(limit: int = 25) -> list[dict]:
    """List all pending applications, newest first."""
    rows = await db.execute_query(
        "SELECT * FROM citizen_applications WHERE status = 'pending' "
        "ORDER BY submitted_at DESC LIMIT $1",
        (limit,),
        fetch_all=True,
    )
    return [dict(r) for r in rows] if rows else []


async def get_application(app_id: int) -> dict | None:
    """Fetch a single application by ID."""
    row = await db.execute_query(
        "SELECT * FROM citizen_applications WHERE id = $1",
        (app_id,),
        fetch_one=True,
    )
    return dict(row) if row else None


async def approve_application(
    app_id: int,
    decided_by_discord_id: str,
    note: str | None = None,
) -> dict | None:
    """Mark an application as approved. Returns the updated row, or None."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE citizen_applications "
            "SET status = 'approved', decided_at = NOW(), "
            "decided_by_discord_id = $1, decision_note = $2 "
            "WHERE id = $3 AND status = 'pending' RETURNING *",
            decided_by_discord_id,
            note,
            app_id,
        )
        return dict(row) if row else None


async def reject_application(
    app_id: int,
    decided_by_discord_id: str,
    note: str | None = None,
) -> dict | None:
    """Mark an application as rejected. Returns the updated row, or None."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE citizen_applications "
            "SET status = 'rejected', decided_at = NOW(), "
            "decided_by_discord_id = $1, decision_note = $2 "
            "WHERE id = $3 AND status = 'pending' RETURNING *",
            decided_by_discord_id,
            note,
            app_id,
        )
        return dict(row) if row else None


async def has_pending_application(applicant_discord_id: str) -> bool:
    """Check if a user already has a pending application."""
    row = await db.execute_query(
        "SELECT id FROM citizen_applications "
        "WHERE applicant_discord_id = $1 AND status = 'pending'",
        (applicant_discord_id,),
        fetch_one=True,
    )
    return row is not None
