"""
Research Group channel archive exporter.

Builds, per Research Group channel, a JSON archive of significant Discord posts
straight from the Discord API over the channel's full history (not from the
vectors, so nothing depends on what was ingested). Meant to be regenerated
weekly and shared with RG hosts for analysis.

What goes in (the same bar the bot's ingest applies, see sync_sig.py):
  - main-channel posts  >= min_chars_orig  (20 default), or any URL, or a star
  - replies             >= min_chars_reply (150 default), or any URL, or a star
  - every non-bot message in non-meeting (discussion) threads
  - meeting threads are left out unless --include-meetings (they have their own
    archive on protocol-institute.org)
  - bot messages are never included

PII: the same standard as the public search results — Discord username and
display name, text as posted; no numeric user ids. Raw <@id> mentions are
rewritten to @username. Attachment CDN URLs are not stored (they expire).

Output (gitignored, under data/): data/rg_archives/<slug>.json, split into
yearly volumes (<slug>-YYYY.json) when the single file would exceed --max-mb,
plus data/rg_archives/index.json listing every file.

Usage:
    python3 ingest/export_rg_archive.py --group mrg
    python3 ingest/export_rg_archive.py --group mrg --dry-run        # fetch + stats, write nothing
    python3 ingest/export_rg_archive.py --all-groups
    python3 ingest/export_rg_archive.py --all-groups --daemon         # self-throttles to weekly
    python3 ingest/export_rg_archive.py --group mrg --no-filter       # every non-bot message
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from utils import clean_text, append_run_log
from sync_sig import (discord_request, fetch_all_threads, fetch_messages_backward,
                      snowflake_to_dt, extract_urls, get_star_count, is_reply,
                      INTER_REQUEST_SLEEP, MANIFEST_PATH)

load_dotenv(Path(__file__).parent.parent / ".env")

SCHEMA_VERSION = 1
OUT_DIR = Path(__file__).parent.parent / "data" / "rg_archives"
STATE_PATH = OUT_DIR / "state.json"
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
WEEKLY = timedelta(days=7)
DEFAULT_MIN_ORIG, DEFAULT_MIN_REPLY = 20, 150

MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


# ── Config ────────────────────────────────────────────────────────────────────

def load_groups(include_archived: bool = False) -> list[dict]:
    manifest = json.loads(MANIFEST_PATH.read_text())
    statuses = {"active", "archived"} if include_archived else {"active"}
    groups = []
    for ch in manifest.get("channels", {}).values():
        if ch.get("type") != "sig" or ch.get("status") not in statuses:
            continue
        groups.append({
            "channel_id": ch["channel_id"],
            "name": ch["name"],
            "display": ch["sig_display"],
            "slug": slugify(ch["sig_display"]),
            "min_chars_orig": ch.get("min_chars_orig", DEFAULT_MIN_ORIG),
            "min_chars_reply": ch.get("min_chars_reply", DEFAULT_MIN_REPLY),
            "meeting_patterns": [re.compile(p, re.I) for p in ch.get("meeting_patterns", [])],
        })
    return groups


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pick_group(groups: list[dict], key: str) -> dict:
    k = key.lower()
    for g in groups:
        if k in (g["slug"], g["display"].lower(), g["name"].lower(), g["channel_id"]):
            return g
    names = ", ".join(g["slug"] for g in groups)
    sys.exit(f"Unknown group '{key}'. Known: {names}")


# ── Message → record ──────────────────────────────────────────────────────────

def rewrite_mentions(content: str, msg: dict, channel_names: dict[str, str]) -> str:
    users = {str(u["id"]): u.get("username", "someone") for u in msg.get("mentions", [])}
    content = MENTION_RE.sub(lambda m: "@" + users.get(m.group(1), "someone"), content)
    content = ROLE_MENTION_RE.sub("@role", content)
    content = CHANNEL_MENTION_RE.sub(lambda m: "#" + channel_names.get(m.group(1), "channel"), content)
    return content


def to_record(msg: dict, group: dict, kind: str, thread: dict | None,
              thread_kind: str | None, channel_names: dict[str, str]) -> dict:
    content = rewrite_mentions(msg.get("content", ""), msg, channel_names)
    text = clean_text(content)
    author = msg.get("author", {})
    ts_dt = snowflake_to_dt(msg["id"])
    location_id = thread["id"] if thread else group["channel_id"]
    ref = msg.get("message_reference") or {}
    rec = {
        "id": msg["id"],
        "kind": kind,
        "timestamp": ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_unix": int(ts_dt.timestamp()),
        "edited_timestamp": msg.get("edited_timestamp"),
        "author": author.get("username", "unknown"),
        "author_display": author.get("global_name") or None,
        "text": text,
        "chars": len(text),
        "urls": extract_urls(text),
        "attachments": [
            {"filename": a.get("filename"), "content_type": a.get("content_type"), "size": a.get("size")}
            for a in msg.get("attachments", [])
        ],
        "reactions": [
            {"emoji": r.get("emoji", {}).get("name"), "count": r.get("count", 0)}
            for r in msg.get("reactions", [])
        ],
        "star_count": get_star_count(msg),
        "pinned": bool(msg.get("pinned")),
        "reply_to": ref.get("message_id"),
        "thread_id": thread["id"] if thread else None,
        "thread_name": thread.get("name") if thread else None,
        "thread_kind": thread_kind,
        "thread_message_count": thread.get("message_count") if thread else None,
        "link": f"https://discord.com/channels/{GUILD_ID}/{location_id}/{msg['id']}",
    }
    return rec


def qualifies(msg: dict, text: str, group: dict) -> bool:
    if msg.get("author", {}).get("bot"):
        return False
    if get_star_count(msg) > 0 or extract_urls(text) or msg.get("attachments"):
        return True
    need = group["min_chars_reply"] if is_reply(msg) else group["min_chars_orig"]
    return len(text) >= need


def is_meeting_thread(name: str, group: dict) -> bool:
    return any(p.search(name or "") for p in group["meeting_patterns"])


# ── Build ─────────────────────────────────────────────────────────────────────

def build_archive(group: dict, include_meetings: bool, no_filter: bool,
                  channel_names: dict[str, str]) -> tuple[dict, list[dict], dict]:
    cid = group["channel_id"]
    print(f"[{group['display']}] #{group['name']} ({cid})")
    print("  fetching main channel history...")
    main_msgs = fetch_messages_backward(cid, cutoff_dt=None)
    print(f"  {len(main_msgs)} main-channel messages")

    print("  fetching thread list...")
    threads = fetch_all_threads(cid)
    threads.sort(key=lambda t: int(t["id"]))
    print(f"  {len(threads)} threads")

    counts = {"main_fetched": len(main_msgs), "main_kept": 0, "main_dropped": 0,
              "threads_total": len(threads), "threads_discussion": 0, "threads_meeting": 0,
              "threads_meeting_skipped": 0, "thread_messages_fetched": 0, "thread_messages_kept": 0,
              "bot_messages_dropped": 0}
    records: list[dict] = []

    for msg in main_msgs:
        if msg.get("author", {}).get("bot"):
            counts["bot_messages_dropped"] += 1
            continue
        text = clean_text(rewrite_mentions(msg.get("content", ""), msg, channel_names))
        if no_filter or qualifies(msg, text, group):
            kind = "reply" if is_reply(msg) else "post"
            records.append(to_record(msg, group, kind, None, None, channel_names))
            counts["main_kept"] += 1
        else:
            counts["main_dropped"] += 1

    thread_index = []
    for t in threads:
        name = t.get("name", "")
        meeting = is_meeting_thread(name, group)
        counts["threads_meeting" if meeting else "threads_discussion"] += 1
        if meeting and not include_meetings:
            counts["threads_meeting_skipped"] += 1
            thread_index.append({"thread_id": t["id"], "name": name, "kind": "meeting",
                                 "message_count": t.get("message_count"), "included": False,
                                 "link": f"https://discord.com/channels/{GUILD_ID}/{t['id']}"})
            continue
        msgs = fetch_messages_backward(t["id"], cutoff_dt=None)
        time.sleep(INTER_REQUEST_SLEEP)
        counts["thread_messages_fetched"] += len(msgs)
        kept = 0
        for msg in msgs:
            if msg.get("author", {}).get("bot"):
                counts["bot_messages_dropped"] += 1
                continue
            text = clean_text(rewrite_mentions(msg.get("content", ""), msg, channel_names))
            if not text and not msg.get("attachments"):
                continue
            records.append(to_record(msg, group, "thread_post", t,
                                     "meeting" if meeting else "discussion", channel_names))
            kept += 1
        counts["thread_messages_kept"] += kept
        thread_index.append({"thread_id": t["id"], "name": name,
                             "kind": "meeting" if meeting else "discussion",
                             "message_count": t.get("message_count"), "included": True,
                             "posts_in_archive": kept,
                             "link": f"https://discord.com/channels/{GUILD_ID}/{t['id']}"})
        print(f"    thread {'[meeting]   ' if meeting else '[discussion]'} {name[:60]!r}: {kept} posts")

    records.sort(key=lambda r: int(r["id"]))
    header = {
        "schema_version": SCHEMA_VERSION,
        "group": group["display"],
        "channel_name": group["name"],
        "channel_id": cid,
        "guild_id": GUILD_ID,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "covers": {"from": records[0]["timestamp"] if records else None,
                   "to": records[-1]["timestamp"] if records else None},
        "selection": ("all non-bot messages" if no_filter else
                      f"main-channel posts >= {group['min_chars_orig']} chars, replies >= "
                      f"{group['min_chars_reply']} chars, or any URL/attachment/star; every non-bot "
                      f"message in discussion threads; meeting threads "
                      f"{'included' if include_meetings else 'listed in `threads` but not included'}; "
                      f"bot messages excluded"),
        "pii_policy": "Same as PIBot public search results: Discord username and display name, "
                      "text as posted; no numeric user ids; <@id> mentions rewritten to @username; "
                      "attachment URLs omitted.",
        "counts": counts,
        "threads": thread_index,
    }
    return header, records, counts


# ── Write ─────────────────────────────────────────────────────────────────────

def dumps(header: dict, posts: list[dict]) -> str:
    return json.dumps({"archive": header, "posts": posts}, ensure_ascii=False, indent=1)


def write_volumes(group: dict, header: dict, records: list[dict], max_mb: float) -> list[dict]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = group["slug"]
    full = dumps(dict(header, posts_in_file=len(records), volume=None), records)
    files = []
    if len(full.encode()) <= max_mb * 1024 * 1024:
        path = OUT_DIR / f"{slug}.json"
        path.write_text(full)
        files.append({"file": path.name, "volume": None, "posts": len(records),
                      "bytes": len(full.encode()), "from": header["covers"]["from"], "to": header["covers"]["to"]})
    else:
        by_year: dict[str, list[dict]] = {}
        for r in records:
            by_year.setdefault(r["timestamp"][:4], []).append(r)
        for year, recs in sorted(by_year.items()):
            h = dict(header, posts_in_file=len(recs), volume=year,
                     covers={"from": recs[0]["timestamp"], "to": recs[-1]["timestamp"]})
            body = dumps(h, recs)
            path = OUT_DIR / f"{slug}-{year}.json"
            path.write_text(body)
            files.append({"file": path.name, "volume": year, "posts": len(recs),
                          "bytes": len(body.encode()), "from": recs[0]["timestamp"], "to": recs[-1]["timestamp"]})
        # remove a stale single-file version if volumes took over
        single = OUT_DIR / f"{slug}.json"
        if single.exists():
            single.unlink()
    return files


def update_index(group: dict, files: list[dict], header: dict):
    path = OUT_DIR / "index.json"
    index = json.loads(path.read_text()) if path.exists() else {"schema_version": SCHEMA_VERSION, "groups": {}}
    index["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index["groups"][group["slug"]] = {
        "group": group["display"], "channel_name": group["name"],
        "generated_at": header["generated_at"], "posts": sum(f["posts"] for f in files),
        "covers": header["covers"], "files": files,
    }
    path.write_text(json.dumps(index, ensure_ascii=False, indent=1))


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", help="slug, display name, channel name or id (e.g. mrg)")
    ap.add_argument("--all-groups", action="store_true")
    ap.add_argument("--include-archived", action="store_true", help="also archived channels (affiliate-chat)")
    ap.add_argument("--include-meetings", action="store_true")
    ap.add_argument("--no-filter", action="store_true", help="every non-bot message, no significance bar")
    ap.add_argument("--max-mb", type=float, default=10.0, help="split into yearly volumes above this size")
    ap.add_argument("--daemon", action="store_true", help="skip unless a week has passed since the last run")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    args = ap.parse_args()
    if not args.group and not args.all_groups:
        ap.error("--group or --all-groups required")
    if not os.environ.get("DISCORD_BOT_TOKEN") or not GUILD_ID:
        sys.exit("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID required in .env")

    state = load_state()
    if args.daemon and state.get("last_run"):
        last = datetime.fromisoformat(state["last_run"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - last < WEEKLY:
            print(f"rg_archive: last run {state['last_run']}, next after {(last + WEEKLY).date()}; skipping")
            return

    groups = load_groups(include_archived=args.include_archived or bool(args.group))
    targets = groups if args.all_groups else [pick_group(groups, args.group)]
    channel_names = {g["channel_id"]: g["name"] for g in groups}
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
        for ch in manifest.get("channels", {}).values():
            channel_names.setdefault(ch["channel_id"], ch["name"])
    except Exception:
        pass

    summary = {}
    for g in targets:
        header, records, counts = build_archive(g, args.include_meetings, args.no_filter, channel_names)
        size_mb = len(dumps(header, records).encode()) / 1024 / 1024
        print(f"  kept {len(records)} posts ({size_mb:.2f} MB): "
              f"main {counts['main_kept']}/{counts['main_fetched']}, "
              f"thread posts {counts['thread_messages_kept']}/{counts['thread_messages_fetched']}, "
              f"discussion threads {counts['threads_discussion']}, meeting threads {counts['threads_meeting']}"
              f"{' (skipped)' if counts['threads_meeting_skipped'] else ''}")
        if args.dry_run:
            summary[g["slug"]] = {"posts": len(records), "mb": round(size_mb, 2)}
            continue
        files = write_volumes(g, header, records, args.max_mb)
        update_index(g, files, header)
        for f in files:
            print(f"  wrote {OUT_DIR / f['file']}  ({f['posts']} posts, {f['bytes']/1024:.0f} KB)")
        summary[g["slug"]] = {"posts": len(records), "files": [f["file"] for f in files]}

    if not args.dry_run:
        state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.setdefault("groups", {}).update({k: v for k, v in summary.items()})
        save_state(state)
        append_run_log({"ts": state["last_run"], "script": "export_rg_archive", "groups": summary})
    print("\n(dry run — nothing written)" if args.dry_run else "\ndone")


if __name__ == "__main__":
    main()
