#!/usr/bin/env python3
"""
City of Newport RI — Granicus feed enumerator + vCon skeleton builder.

Reads the city's public Granicus video feed, selects meetings, and writes one
spec-compliant vCon skeleton per meeting (metadata + lawful basis + a video
dialog pointing at the Granicus download URL). Transcripts are added later by
transcribe_newport.py.

The IETF dataset used the external `ietf2vcon` generator for this step; here we
inline the equivalent for the Granicus source.

Feed:  https://cityofnewport.granicus.com/ViewPublisherRSS.php?view_id=1&mode=vpodcast
Each <item> carries a title, pubDate, and a clip_id embedded in the
MediaPlayer.php link and the DownloadFile.php enclosure.

Usage:
    python fetch_feed.py --body citycouncil --limit 5
    python fetch_feed.py --body citycouncil --dry-run
    python fetch_feed.py --all --limit 20         # all bodies, 20 most recent
    python fetch_feed.py --list-bodies            # show body slugs seen in feed
"""

import argparse
import json
import re
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED_URL = "https://cityofnewport.granicus.com/ViewPublisherRSS.php?view_id=1&mode=vpodcast"
PORTAL_BASE = "https://cityofnewport.granicus.com"
VIEW_ID = 1

# How long the dataset retains the right to process the public recording.
RETENTION_EXPIRATION = "2099-12-31T23:59:59+00:00"
# RI Open Meetings Act — recordings of public meetings are public record.
OPEN_MEETINGS_ACT = "http://webserver.rilegislature.gov/Statutes/TITLE42/42-46/INDEX.htm"


def fetch_feed(url: str = FEED_URL) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "vcon-dataset-newport/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def slugify_body(title: str) -> str:
    """Derive a body slug from a meeting title.

    e.g. "Newport City Council Regular Meeting - June 25, 2025" -> "citycouncil"
         "Planning Board Regular Meeting" -> "planningboard"
    """
    t = title.lower()
    # Strip a leading "newport " and the city qualifier for matching.
    known = [
        ("citycouncil", ["city council"]),
        ("planningboard", ["planning board"]),
        ("zoningboard", ["zoning board", "zoning board of review"]),
        ("hdc", ["historic district commission", "historic district"]),
        ("schoolcommittee", ["school committee"]),
        ("licensecommission", ["board of license", "license commission"]),
        ("canvassing", ["canvassing"]),
        ("waterfront", ["waterfront commission"]),
        ("tree", ["tree commission"]),
    ]
    for slug, needles in known:
        if any(n in t for n in needles):
            return slug
    # Fallback: first few words, alnum only.
    base = re.sub(r"[^a-z0-9]+", "", t.split(" meeting")[0])
    return base[:24] or "meeting"


def parse_meeting_date(title: str, pub_dt: datetime) -> datetime:
    """Prefer a date written in the title (the actual meeting date); fall back
    to the feed pubDate (which is the archival timestamp)."""
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
        title,
    )
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return pub_dt


def clip_id_from(text: str) -> str | None:
    m = re.search(r"clip_id=(\d+)", text or "")
    return m.group(1) if m else None


def parse_items(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        enclosure = item.find("enclosure")
        enc_url = enclosure.get("url") if enclosure is not None else None

        clip_id = clip_id_from(link) or clip_id_from(enc_url)
        if not clip_id:
            continue

        try:
            pub_dt = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except (TypeError, ValueError):
            pub_dt = None
        if pub_dt is None:
            pub_dt = datetime.now(timezone.utc)

        meeting_dt = parse_meeting_date(title, pub_dt)
        items.append({
            "title": title,
            "clip_id": clip_id,
            "body": slugify_body(title),
            "meeting_dt": meeting_dt,
            "pub_dt": pub_dt,
            "player_url": link or f"{PORTAL_BASE}/MediaPlayer.php?view_id={VIEW_ID}&clip_id={clip_id}",
            "download_url": enc_url or f"{PORTAL_BASE}/DownloadFile.php?view_id={VIEW_ID}&clip_id={clip_id}",
        })
    return items


def build_skeleton(item: dict) -> dict:
    """Build a spec-compliant vCon skeleton (no analysis yet).

    Structure verified against draft-ietf-vcon-vcon-core and the
    ietf-meeting-vcons template. Attachment `body` fields are JSON strings
    paired with encoding:"json"; attachments carry purpose + party + dialog.
    """
    now = datetime.now(timezone.utc).isoformat()
    meeting_iso = item["meeting_dt"].isoformat()
    subject = item["title"] or f"Newport meeting clip {item['clip_id']}"

    meeting_metadata = {
        "source": "City of Newport, RI — Granicus public meeting portal",
        "portal": PORTAL_BASE,
        "granicus_view_id": VIEW_ID,
        "granicus_clip_id": item["clip_id"],
        "meeting_body": item["body"],
        "meeting_date": item["meeting_dt"].date().isoformat(),
        "title": item["title"],
    }

    lawful_basis = {
        "lawful_basis": "legal_obligation",
        "expiration": RETENTION_EXPIRATION,
        "purpose_grants": [
            {"purpose": "recording", "granted": True, "granted_at": now},
            {"purpose": "transcription", "granted": True, "granted_at": now},
            {"purpose": "publication", "granted": True, "granted_at": now},
        ],
        "terms_of_service": OPEN_MEETINGS_ACT,
        "metadata": {
            "terms_of_service_name": "Rhode Island Open Meetings Act (R.I. Gen. Laws ch. 42-46)",
            "jurisdiction": "Rhode Island, USA",
            "controller": "City of Newport, Rhode Island",
            "notes": (
                "Recording of an open public meeting, published by the City of Newport "
                "as a public record. Captured and transcribed for the public vCon dataset."
            ),
        },
    }

    return {
        "vcon": "0.4.0",
        "uuid": str(uuid.uuid4()),
        "created_at": now,
        "updated_at": now,
        "subject": subject,
        "extensions": ["lawful_basis", "wtf_transcription"],
        "parties": [
            {"name": "City of Newport, RI", "role": "convener"},
            {"name": "Public Attendees", "role": "attendee"},
        ],
        "dialog": [
            {
                "type": "recording",
                "start": meeting_iso,
                "parties": [0, 1],
                "mediatype": "video/mp4",
                "url": item["download_url"],
                "meta": {
                    "granicus_clip_id": item["clip_id"],
                    "player_url": item["player_url"],
                    "meeting_body": item["body"],
                },
            }
        ],
        "attachments": [
            {
                "purpose": "meeting_metadata",
                "party": 0,
                "dialog": 0,
                "encoding": "json",
                "start": now,
                "body": json.dumps(meeting_metadata),
            },
            {
                "purpose": "lawful_basis",
                "party": 0,
                "dialog": 0,
                "encoding": "json",
                "start": now,
                "body": json.dumps(lawful_basis),
            },
        ],
        "analysis": [],
    }


def vcon_filename(item: dict) -> str:
    date = item["meeting_dt"].date().isoformat()
    return f"newport_{item['body']}_{date}_{item['clip_id']}.vcon.json"


def main():
    ap = argparse.ArgumentParser(description="Build Newport RI vCon skeletons from the Granicus feed")
    ap.add_argument("--body", help="Body slug to keep (e.g. citycouncil, planningboard). Omit with --all.")
    ap.add_argument("--all", action="store_true", help="Include all meeting bodies")
    ap.add_argument("--limit", type=int, default=None, help="Max meetings to write (most recent first; default: all)")
    ap.add_argument("--outdir", help="Output directory (default: <repo>/<body or 'meetings'>)")
    ap.add_argument("--dry-run", action="store_true", help="List selected meetings without writing")
    ap.add_argument("--list-bodies", action="store_true", help="Print body slugs seen in the feed and exit")
    ap.add_argument("--force", action="store_true", help="Overwrite an existing vCon file (default: skip)")
    args = ap.parse_args()

    if not args.body and not args.all and not args.list_bodies:
        ap.error("specify --body <slug>, or --all, or --list-bodies")

    print(f"Fetching feed: {FEED_URL}")
    items = parse_items(fetch_feed())
    items.sort(key=lambda x: x["meeting_dt"], reverse=True)
    print(f"  {len(items)} items in feed")

    if args.list_bodies:
        from collections import Counter
        for slug, n in Counter(i["body"] for i in items).most_common():
            print(f"  {slug:20s} {n}")
        return

    if args.body:
        items = [i for i in items if i["body"] == args.body]
        print(f"  {len(items)} match body '{args.body}'")

    if args.limit is not None:
        items = items[: args.limit]
        print(f"  selecting {len(items)} most recent")

    repo = Path(__file__).resolve().parent.parent
    fixed_outdir = Path(args.outdir) if args.outdir else None

    written = 0
    for item in items:
        # Organise by meeting body unless an explicit --outdir was given.
        outdir = fixed_outdir if fixed_outdir else repo / item["body"]
        outdir.mkdir(parents=True, exist_ok=True)
        fname = vcon_filename(item)
        path = outdir / fname
        flag = ""
        if path.exists() and not args.force:
            flag = " [exists, skip]"
        elif not args.dry_run:
            path.write_text(json.dumps(build_skeleton(item), indent=2, ensure_ascii=False), encoding="utf-8")
            written += 1
            flag = " [written]"
        print(f"  {item['meeting_dt'].date()}  {item['body']:14s} clip {item['clip_id']:>5}  {flag}")

    if args.dry_run:
        print("\nDry run — nothing written.")
    else:
        print(f"\nWrote {written} vCon skeleton(s) to {outdir}")


if __name__ == "__main__":
    main()
