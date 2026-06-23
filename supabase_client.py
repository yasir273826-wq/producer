"""
supabase_client.py
─────────────────────────────────────────────────────────────────────────────
Shared Supabase helper — copy this file verbatim into BOTH the producer repo
and the uploader repo.  Both repos are separate GitHub repos with no shared
import path, so the only safe way to share code is to literally duplicate
this file.

Wraps every DATABASE (metadata-only) operation the pipeline needs via the
official `supabase-py` library. Clip .mp4 file storage has moved to
Backblaze B2 — see b2_client.py. This file no longer touches Supabase
Storage at all; Supabase is now Postgres-only for this pipeline.

Env vars required (set as GitHub Secrets in both repos):
  SUPABASE_URL          – Project URL, e.g. https://xyzxyz.supabase.co
  SUPABASE_SERVICE_KEY  – service_role key (full DB access)
                          ⚠️  Never commit this — add to GitHub Secrets only.

Tables (see schema.sql):
  processed_videos  – one row per YouTube video ID that has been downloaded
  clips             – one row per rendered clip (pending/uploaded);
                      storage_path now refers to an object key in the
                      Backblaze B2 bucket, not Supabase Storage.
  daily_slots       – one row per (date, slot) pair, the double-upload guard
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from supabase import create_client, Client

PKT = ZoneInfo("Asia/Karachi")

TARGET_SLOTS = [
    ("06:00_AM", 6, 0),
    ("10:00_AM", 10, 0),
    ("02:00_PM", 14, 0),
    ("06:00_PM", 18, 0),
    ("09:00_PM", 21, 0),
]


def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must both be set as "
            "GitHub Secrets (Settings → Secrets → Actions)."
        )
    return create_client(url, key)


# ─── time helpers ────────────────────────────────────────────────────────────

def now_pkt() -> datetime:
    return datetime.now(PKT)


def today_str() -> str:
    return now_pkt().strftime("%Y-%m-%d")


def closest_slot(now: datetime, tolerance_minutes: int = 20) -> str | None:
    best_slot = None
    best_diff = None
    for slot_name, hour, minute in TARGET_SLOTS:
        slot_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        diff_minutes = abs((now - slot_dt).total_seconds()) / 60.0
        if diff_minutes <= tolerance_minutes:
            if best_diff is None or diff_minutes < best_diff:
                best_diff = diff_minutes
                best_slot = slot_name
    return best_slot


# ─── processed_videos table ──────────────────────────────────────────────────

def is_video_processed(sb: Client, video_id: str) -> bool:
    res = (
        sb.table("processed_videos")
        .select("video_id")
        .eq("video_id", video_id)
        .limit(1)
        .execute()
    )
    return len(res.data) > 0


def get_processed_video_ids(sb: Client) -> set[str]:
    res = sb.table("processed_videos").select("video_id").execute()
    return {row["video_id"] for row in res.data}


def mark_video_processed(sb: Client, video_id: str) -> None:
    sb.table("processed_videos").upsert(
        {"video_id": video_id, "processed_at": datetime.utcnow().isoformat()}
    ).execute()


# ─── clips table ─────────────────────────────────────────────────────────────

def insert_clip(sb: Client, clip: dict) -> None:
    """
    Insert a new clip row.  clip dict must have:
      clip_id, source_video_id, part_number, total_parts, storage_path
    status defaults to 'pending', uploaded_on_date to NULL.
    """
    sb.table("clips").insert(
        {
            "clip_id": clip["clip_id"],
            "source_video_id": clip["source_video_id"],
            "part_number": clip["part_number"],
            "total_parts": clip["total_parts"],
            "storage_path": clip["storage_path"],
            "status": "pending",
            "uploaded_on_date": None,
        }
    ).execute()


def get_pending_clips_grouped_by_source(sb: Client) -> dict[str, list[dict]]:
    """
    Returns all non-uploaded clips grouped by source_video_id.
    Shape: { source_video_id: [clip_dict, ...] }
    Used by uploader's pick_next_clip().
    """
    res = (
        sb.table("clips")
        .select("*")
        .neq("status", "uploaded")
        .execute()
    )
    grouped: dict[str, list[dict]] = {}
    for row in res.data:
        grouped.setdefault(row["source_video_id"], []).append(row)
    return grouped


def get_all_clips(sb: Client) -> list[dict]:
    """Return every clip row — used for queue-depth checks in producer."""
    res = sb.table("clips").select("*").execute()
    return res.data


def count_pending_clips(sb: Client) -> int:
    res = (
        sb.table("clips")
        .select("clip_id", count="exact")
        .eq("status", "pending")
        .execute()
    )
    return res.count or 0


def mark_clip_uploaded(sb: Client, clip_id: str, uploaded_on_date: str) -> None:
    sb.table("clips").update(
        {"status": "uploaded", "uploaded_on_date": uploaded_on_date}
    ).eq("clip_id", clip_id).execute()


def get_uploaded_today_sources(sb: Client, today: str) -> set[str]:
    """Return source_video_ids that already had a clip uploaded today."""
    res = (
        sb.table("clips")
        .select("source_video_id")
        .eq("status", "uploaded")
        .eq("uploaded_on_date", today)
        .execute()
    )
    return {row["source_video_id"] for row in res.data}


# ─── daily_slots table ───────────────────────────────────────────────────────

def is_slot_uploaded(sb: Client, date_str: str, slot_name: str) -> bool:
    res = (
        sb.table("daily_slots")
        .select("uploaded")
        .eq("date", date_str)
        .eq("slot_name", slot_name)
        .limit(1)
        .execute()
    )
    if not res.data:
        return False
    return bool(res.data[0]["uploaded"])


def mark_slot_uploaded(sb: Client, date_str: str, slot_name: str) -> None:
    sb.table("daily_slots").upsert(
        {"date": date_str, "slot_name": slot_name, "uploaded": True}
    ).execute()


def ensure_today_slots(sb: Client) -> None:
    """
    Make sure all 5 slots for today exist in daily_slots (uploaded=False).
    Uses upsert with ignore_duplicates so it's idempotent and race-safe.
    """
    today = today_str()
    rows = [
        {"date": today, "slot_name": slot_name, "uploaded": False}
        for slot_name, _, _ in TARGET_SLOTS
    ]
    sb.table("daily_slots").upsert(rows, ignore_duplicates=True).execute()
