"""
youtube_api.py
─────────────────────────────────────────────────────────────────────────────
Wraps the official YouTube Data API v3 for everything that used to be done
with per-video yt-dlp metadata lookups — channel resolution, video listing,
and upload-date resolution.

WHY THIS EXISTS:
yt-dlp's per-video metadata lookups (one yt-dlp process per candidate video,
just to read its upload date) consistently triggered YouTube's "Sign in to
confirm you're not a bot" defense on GitHub Actions runners, even with valid
browser-exported cookies. The official Data API has no such bot-detection —
it's a normal authenticated REST API call with a quota, not a scraping
target. yt-dlp is still used downstream for the actual video DOWNLOAD (a
single heavy operation per video, not 80 rapid-fire lookups), where cookies
work fine.

QUOTA NOTE: every call below costs 1 unit (channels.list, playlistItems.list,
videos.list are all "list" operations = 1 unit each, batched up to 50 items
per call). A full daily run (resolve handle + list ~80 videos + fetch
metadata for ~80 videos) costs roughly 5-6 units total, against a 10,000
unit/day free quota — this could run many times a day without any risk of
exhausting the quota.

Env vars expected:
  YOUTUBE_API_KEY  – an API key with the "YouTube Data API v3" enabled,
                      created in Google Cloud Console (required).
"""

import os
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

API_SERVICE_NAME = "youtube"
API_VERSION = "v3"


def _get_client():
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "YOUTUBE_API_KEY is not set. Create one in Google Cloud Console "
            "(APIs & Services -> Credentials -> Create API Key) with the "
            "'YouTube Data API v3' enabled, then add it as a GitHub secret."
        )
    return build(API_SERVICE_NAME, API_VERSION, developerKey=api_key)


def resolve_channel(channel_input: str) -> dict | None:
    """
    Resolve a channel handle (e.g. "@khizaromer"), a bare name, or a full
    channel ID (UC...) into {"channel_id": ..., "uploads_playlist_id": ...}.

    Every YouTube channel has an "uploads" playlist that contains all of
    its public videos in upload order — fetching THAT playlist is the
    standard, efficient way to list a channel's uploads via the API
    (cheaper and simpler than search.list, which costs 100 units/call).
    """
    youtube = _get_client()
    channel_input = channel_input.strip()

    try:
        if channel_input.startswith("UC") and len(channel_input) == 24:
            # Looks like a raw channel ID already.
            resp = youtube.channels().list(part="contentDetails", id=channel_input).execute()
        elif channel_input.startswith("@"):
            resp = youtube.channels().list(part="contentDetails", forHandle=channel_input).execute()
        else:
            # Try as a handle without the @ first (API accepts both forms
            # inconsistently depending on version), falling back to forUsername
            # for legacy custom URLs.
            resp = youtube.channels().list(part="contentDetails", forHandle=f"@{channel_input}").execute()
            if not resp.get("items"):
                resp = youtube.channels().list(part="contentDetails", forUsername=channel_input).execute()
    except HttpError as e:
        print(f"::error::YouTube API channel lookup failed: {e}")
        return None

    items = resp.get("items", [])
    if not items:
        return None

    channel_id = items[0]["id"]
    uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    return {"channel_id": channel_id, "uploads_playlist_id": uploads_playlist_id}


def list_uploaded_video_ids(uploads_playlist_id: str, max_results: int = 80) -> list[str]:
    """
    Page through a channel's uploads playlist (newest first) and return up
    to `max_results` video IDs. 1 API unit per page of up to 50 items.
    """
    youtube = _get_client()
    video_ids: list[str] = []
    page_token = None

    while len(video_ids) < max_results:
        try:
            resp = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=min(50, max_results - len(video_ids)),
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            print(f"::error::YouTube API playlistItems.list failed: {e}")
            break

        for item in resp.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return video_ids[:max_results]


def fetch_video_metadata(video_ids: list[str]) -> dict[str, dict]:
    """
    Fetch {video_id: {"upload_date": "YYYYMMDD", "title": str,
    "duration_seconds": float}} for a list of video IDs, batched 50 at a
    time (the API max per videos.list call) — costs 1 unit per batch of 50,
    NOT 1 unit per video.
    """
    youtube = _get_client()
    metadata: dict[str, dict] = {}

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            resp = youtube.videos().list(
                part="snippet,contentDetails",
                id=",".join(batch),
            ).execute()
        except HttpError as e:
            print(f"::error::YouTube API videos.list failed for batch starting at {i}: {e}")
            continue

        for item in resp.get("items", []):
            vid = item["id"]
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})

            published_at = snippet.get("publishedAt")  # ISO 8601, e.g. 2026-06-21T10:00:00Z
            upload_date = None
            if published_at:
                try:
                    dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    upload_date = dt.strftime("%Y%m%d")
                except ValueError:
                    upload_date = None

            metadata[vid] = {
                "upload_date": upload_date,
                "title": snippet.get("title", ""),
                "duration_iso8601": content_details.get("duration", ""),
            }

    return metadata
