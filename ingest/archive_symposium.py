"""
Build the publishable archive of Protocol Symposium 2026: slide PDFs, recording
transcripts, and a manifest that says which talk lives on which resource page.

c3po owns ingestion of archival material and the publishing pipe to both sites
(VGR, session 60). The canonical home of each talk is its resource page on
protocolized.io; protocol-institute.org embeds the video and links there. This
script produces the data only. Rendering belongs to the sites:

    protocolized-website  scripts/sync-symposium-archive.py   reads the manifest +
                          transcripts, uploads the PDFs to R2, writes resource bodies
    website               data/symposium-2026-archive.json    (written here, --website)
                          tells the programme page where each talk's resource page is

Outputs:
    sources/symposium/archive.json             manifest (committed)
    sources/symposium/transcripts/<vid>.json   paragraphed transcripts (committed)
    sources/symposium/slides/<name>.pdf        slide/paper PDFs (gitignored; go to R2)

Inputs:
    config/symposium_web_decks.json   decks published as web pages (talk-kit player),
                                      printed slide by slide from the built site
    data/symposium_decks_state.json   which Drive file belongs to which talk — the
                                      matching, duplicate and stub verdicts of
                                      sync_symposium_decks.py are reused, not redone
    sources/youtube/{video_meta,enriched_meta}.json, captions_timed/
    protocol-institute.org /api/symposium/proposals and
        /data/symposium-2026-recordings.json (which part of which video holds which
        talk; the Opening Talk upload holds three programme items)
    protocolized-website src/content/resources/   existing resource slugs, by video id

PII: decks carry contact slides and papers carry author emails. Every email
address is redacted in the PDF itself (PyMuPDF redaction removes the text, not
just covers it) and the redacted file is re-checked. A PDF with no text layer
cannot be checked that way and is listed as needs_visual_check — look at it
before publishing.

Laptop only, like sync_symposium_videos.py: it needs LibreOffice (pptx/docx ->
PDF), Chrome (html -> PDF, and web decks via Playwright), the sibling talk-site
builds, and the local caption cache.

    python3 ingest/archive_symposium.py --dry-run       # report, write nothing
    python3 ingest/archive_symposium.py                 # build PDFs + manifest + transcripts
    python3 ingest/archive_symposium.py --website ../website   # also write the .org data file
"""

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
from sync_symposium import fetch_json, hosts_of, EVENT_NAME, BASE_URL, EVENT_PATH
from sync_symposium_decks import (
    ROOT_FOLDER, GSLIDES, GDOC, DRIVE_LINK_RE, ANY_LINK_RE, POINTER_THIN_CHARS, EMAIL_RE,
    _get, walk_folder, resolve_shortcuts,
)

ROOT        = Path(__file__).parent.parent
OUT         = ROOT / "sources" / "symposium"
SLIDES_DIR  = OUT / "slides"
TRANS_DIR   = OUT / "transcripts"
MANIFEST    = OUT / "archive.json"
DECKS_STATE = ROOT / "data" / "symposium_decks_state.json"
WEB_DECKS   = ROOT / "config" / "symposium_web_decks.json"
YT_DIR      = ROOT / "sources" / "youtube"
TIMED_DIR   = YT_DIR / "captions_timed"
SERIES      = "symposium-2026"

PROTOCOLIZED     = "https://protocolized.io"
RECORDINGS_PATH  = "/data/symposium-2026-recordings.json"
WEBSITE_DATA     = "data/symposium-2026-archive.json"

SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
CHROME  = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Paragraph shaping, the same rule the website's transcript pages used: close at
# a speaker change (">>"), after a pause at a sentence end, or at the first
# sentence end once a paragraph has run about a minute.
PARA_SECONDS = 60
GAP_SECONDS  = 4
SENTENCE_END = re.compile(r"[.?!][\"')\]]?$")


def slugify(text: str) -> str:
    """protocolized-website's slug rule (scripts/sync-youtube-resources.py)."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:80]


def load(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ── Transcripts ───────────────────────────────────────────────────────────────

def caption_lines(vid: str) -> list[tuple[int, str]]:
    """Timed caption lines, split wherever a speaker-change marker falls mid-line."""
    out = []
    for t, text in load(TIMED_DIR / f"{vid}.json", []):
        for i, part in enumerate(re.split(r"(?=>>)", text)):
            if part.strip():
                out.append((int(t), part.strip()))
    return out


def paragraphs(lines, start=0, end=None) -> list[list]:
    paras, cur, cur_start, last = [], [], None, None

    def close():
        nonlocal cur, cur_start
        text = re.sub(r"\s+", " ", " ".join(cur)).strip()
        if text:
            paras.append([cur_start, text])
        cur, cur_start = [], None

    for t, text in lines:
        if t < start or (end is not None and t >= end):
            continue
        if text.startswith(">>"):
            close()
            text = text[2:].strip()
            if not text:
                last = t
                continue
        elif last is not None and t - last >= GAP_SECONDS and cur and SENTENCE_END.search(cur[-1]):
            close()
        if cur_start is None:
            cur_start = t
        cur.append(text)
        last = t
        if t - cur_start >= PARA_SECONDS and SENTENCE_END.search(text):
            close()
    close()
    return paras


# ── PDFs ──────────────────────────────────────────────────────────────────────

def convert(blob: bytes, suffix: str) -> bytes:
    """pptx/ppt/docx via LibreOffice, html via headless Chrome."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in{suffix}"
        src.write_bytes(blob)
        if suffix in (".html", ".htm"):
            out = Path(d) / "out.pdf"
            subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={out}", src.as_uri()],
                           capture_output=True, timeout=180, check=True)
        else:
            subprocess.run([SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", d, str(src)],
                           capture_output=True, timeout=300, check=True)
            out = Path(d) / "in.pdf"
        if not out.exists():
            raise RuntimeError(f"conversion of {suffix} produced no PDF")
        return out.read_bytes()


JUNK = re.compile(r"(^|/)(__MACOSX|\.DS_Store$)")


def zip_member(blob: bytes) -> tuple[str, bytes] | None:
    """The one member of a bundle worth publishing: a PDF if there is one, else
    the largest deck. Bundles hold renderings of the same deck several times.
    None for a bundle with no deck in it (code, schemas, notes)."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if not n.endswith("/") and not JUNK.search(n)]
    for exts in ((".pdf",), (".pptx", ".ppt"), (".docx",), (".html", ".htm")):
        hits = [n for n in names if n.lower().endswith(exts)]
        if hits:
            best = max(hits, key=lambda n: z.getinfo(n).file_size)
            return best, z.read(best)
    return None


def clean_bundle(blob: bytes) -> bytes:
    """A deck-less bundle is published as the zip itself, minus Finder debris.
    Every member is text here (markdown, JSON, JS); any email address fails it."""
    src, buf = zipfile.ZipFile(io.BytesIO(blob)), io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            if info.is_dir() or JUNK.search(info.filename):
                continue
            data = src.read(info)
            if EMAIL_RE.search(data.decode("utf-8", "replace")):
                raise RuntimeError(f"email address in bundle member {info.filename}")
            out.writestr(info.filename, data)
    return buf.getvalue()


def to_pdf(f: dict) -> tuple[bytes, str]:
    """(pdf bytes, how) for one Drive file — or (zip bytes, "bundle") for a
    bundle with no deck in it."""
    fid, mime, name = f["id"], f.get("mime", ""), f["name"].lower()
    if mime == GSLIDES:
        return _get(f"https://docs.google.com/presentation/d/{fid}/export/pdf"), "slides export"
    if mime == GDOC:
        return _get(f"https://docs.google.com/document/d/{fid}/export?format=pdf"), "docs export"
    blob = _get(f"https://drive.google.com/uc?export=download&id={fid}")
    if blob[:4] == b"%PDF":
        return blob, "as uploaded"
    if blob[:2] == b"PK" and (name.endswith(".zip") or "zip" in mime):
        picked = zip_member(blob)
        if picked is None:
            return clean_bundle(blob), "bundle"
        member, data = picked
        if data[:4] == b"%PDF":
            return data, f"zip member {member}"
        return convert(data, Path(member).suffix.lower()), f"zip member {member}, converted"
    suffix = Path(name).suffix or (".pptx" if "presentation" in mime else ".docx")
    return convert(blob, suffix), f"converted from {suffix}"


def pdf_text(pdf: bytes) -> str:
    import pymupdf
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc)


def follow_pdf_pointer(pdf: bytes) -> tuple[str, str] | None:
    """A one-slide deck whose only content is a link to the real slides (one hop).

    ("drive", id, mime) for a Drive document, which is archived in its place;
    ("web", url, "") for anything else, which is linked rather than copied —
    a slides web page does not print to a faithful PDF."""
    text = pdf_text(pdf)
    if len(text.strip()) >= POINTER_THIN_CHARS:
        return None
    m = DRIVE_LINK_RE.search(text)
    if m:
        kind, fid = m.groups()
        return "drive", fid, {"presentation": GSLIDES, "document": GDOC}.get(kind, "")
    m = ANY_LINK_RE.search(text)
    return ("web", m.group(0).rstrip(".,);"), "") if m else None


def redact_emails(pdf: bytes) -> tuple[bytes, int, int, bool]:
    """(pdf, pages, emails redacted, has text layer). Raises if any survive."""
    import pymupdf
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    found, chars = 0, 0
    for page in doc:
        text = page.get_text()
        chars += len(text.strip())
        hits = set(EMAIL_RE.findall(text))
        for addr in hits:
            for rect in page.search_for(addr):
                page.add_redact_annot(rect, fill=(1, 1, 1))
                found += 1
        if hits:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
    pages = len(doc)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    left = EMAIL_RE.findall(pdf_text(out))
    if left:
        raise RuntimeError(f"{len(left)} email address(es) survived redaction")
    return out, pages, found, chars >= 40 * max(pages, 1)


def export_web_deck(root: Path, path: str) -> bytes:
    """Print a talk-kit web deck one slide per page, as it was projected.

    Serves the built site (its asset paths are site-absolute), steps the player
    with the arrow key, and prints only #stage each time — real text, not
    screenshots. Uses the installed Chrome; no browser download."""
    import functools, http.server, threading
    import pymupdf
    from playwright.sync_api import sync_playwright

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    w, h = 1600, 900
    css = f"""@page {{ size: {w}px {h}px; margin: 0; }}
      html, body {{ margin: 0 !important; padding: 0 !important; }}
      body * {{ visibility: hidden !important; }}
      #stage, #stage * {{ visibility: visible !important; }}
      #stage {{ position: fixed !important; left: 0; top: 0; width: {w}px !important;
               height: {h}px !important; max-width: none !important; margin: 0 !important;
               box-sizing: border-box; }}"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            pg = browser.new_page(viewport={"width": w, "height": h})
            pg.goto(f"http://127.0.0.1:{srv.server_port}{path}", wait_until="networkidle")
            n = pg.evaluate("DECK.length")
            pg.add_style_tag(content=css)
            pg.emulate_media(media="screen")
            doc = pymupdf.open()
            for i in range(n):
                if i:
                    pg.keyboard.press("ArrowRight")
                pg.wait_for_timeout(250)
                one = pg.pdf(width=f"{w}px", height=f"{h}px", print_background=True,
                             page_ranges="1")
                doc.insert_pdf(pymupdf.open(stream=one, filetype="pdf"))
            browser.close()
            return doc.tobytes(garbage=3, deflate=True)
    finally:
        srv.shutdown()


def doc_title(name: str) -> str:
    stem = re.sub(r"\.(pdf|pptx?|docx?|html?|zip)$", "", name, flags=re.I)
    return re.sub(r"[_\s]+", " ", stem).strip()


# ── Inputs ────────────────────────────────────────────────────────────────────

def programme() -> dict:
    props = fetch_json("/api/symposium/proposals").get("proposals", [])
    return {p["slug"]: {
        "proposal_id": p["id"], "slug": p["slug"], "title": p["title"].strip(),
        "type": p.get("type"), "speakers": hosts_of(p), "date": p.get("scheduled_date"),
        "abstract": (p.get("abstract") or "").strip(),
    } for p in props if p.get("slug")}


def org_recordings() -> list[dict]:
    req = Request(BASE_URL + RECORDINGS_PATH, headers={"User-Agent": "c3po-ingest/1.0"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("recordings", [])


def existing_resources(root: Path) -> tuple[dict, set]:
    """({video id: resource slug}, every slug) from protocolized-website's library."""
    yt = re.compile(r'url:\s*"https://www\.youtube\.com/watch\?v=([^"&]+)"')
    by_vid, slugs = {}, set()
    for p in sorted((root / "src" / "content" / "resources").glob("*.md")):
        slugs.add(p.stem)
        m = yt.search(p.read_text(encoding="utf-8"))
        if m:
            by_vid.setdefault(m.group(1), p.stem)
    return by_vid, slugs


# ── Build ─────────────────────────────────────────────────────────────────────

def build(protocolized: Path, dry_run=False, force=False):
    prog = programme()
    by_id = {t["proposal_id"]: t for t in prog.values()}
    meta = load(YT_DIR / "video_meta.json", {})
    enriched = load(YT_DIR / "enriched_meta.json", {})
    vids = {k: v for k, v in meta.items() if v.get("series") == SERIES}
    res_by_vid, io_slugs = existing_resources(protocolized)
    old = load(MANIFEST, {}) or {}
    old_slides = {s["file_id"]: s for r in old.get("resources", {}).values() for s in r.get("slides", [])}

    # Which part of which video holds which programme item. The .org mapping has
    # the split points; c3po's own video matching covers anything it lacks.
    parts: dict[str, list] = {}
    for r in org_recordings():
        t = by_id.get(r["proposal_id"])
        if t:
            parts.setdefault(r["video_id"], []).append(
                {"talk": t["slug"], "start": r.get("start", 0), "end": r.get("end")})
    vstate = load(ROOT / "data" / "symposium_videos_state.json", {}).get("videos", {})
    for vid in vids:
        if vid not in parts and vstate.get(vid, {}).get("slug") in prog:
            parts[vid] = [{"talk": vstate[vid]["slug"], "start": 0, "end": None}]

    resources: dict[str, dict] = {}
    talk_res: dict[str, str] = {}
    for vid, v in sorted(vids.items(), key=lambda kv: kv[1].get("title", "")):
        slug = res_by_vid.get(vid)
        new = slug is None
        if new:
            slug = slugify(v["title"])
            if slug in io_slugs:
                slug = slug[:76] + "-yt"
        e = enriched.get(vid, {})
        talks = sorted(parts.get(vid, []), key=lambda p: p["start"] or 0)
        resources[slug] = {
            "slug": slug, "new": new, "kind": "recording",
            "title": v["title"], "video_id": vid,
            "video_url": f"https://www.youtube.com/watch?v={vid}",
            "duration_sec": v.get("duration_sec"),
            "date": v.get("date") or (prog[talks[0]["talk"]]["date"] if talks else None),
            "speakers": e.get("speakers") or [],
            "description": e.get("summary") or "",
            "talks": talks, "slides": [], "transcript": None,
        }
        for p in talks:
            talk_res.setdefault(p["talk"], slug)

    # Decks: the deck sync's verdicts are authoritative. Duplicates and stubs
    # never reach its state with a slug; also_slugs files serve two talks.
    decks = load(DECKS_STATE, {}).get("files", {})
    drive = {f["id"]: f for f in resolve_shortcuts(walk_folder(ROOT_FOLDER))}
    report = {"built": 0, "reused": 0, "failed": [], "visual_check": [], "redacted": 0}
    if not dry_run:
        SLIDES_DIR.mkdir(parents=True, exist_ok=True)

    def resource_for(talk: str) -> str:
        """The resource page a talk's slides go on: its recording's page, or a
        page of its own when it was not recorded."""
        slug = talk_res.get(talk)
        if slug:
            return slug
        t = prog[talk]
        slug = slugify(t["title"])
        if slug in res_by_vid.values():
            slug = slug[:73] + "-slides"   # never land on a recording's page
        resources.setdefault(slug, {
            "slug": slug, "new": slug not in io_slugs, "kind": "slides",
            "title": t["title"], "video_id": None, "video_url": None,
            "duration_sec": None, "date": t["date"], "speakers": t["speakers"],
            "description": t["abstract"], "talks": [{"talk": talk, "start": 0, "end": None}],
            "slides": [], "transcript": None,
        })
        talk_res[talk] = slug
        return slug

    for fid, st in sorted(decks.items(), key=lambda kv: kv[1].get("name", "")):
        talk = st.get("slug")
        if not talk or st.get("duplicate_of"):
            continue
        f = drive.get(fid)
        if not f:
            report["failed"].append((st.get("name"), "no longer in the Drive folder"))
            continue
        t = prog.get(talk)
        if not t:
            report["failed"].append((f["name"], f"talk {talk} not in programme"))
            continue
        slug = resource_for(talk)

        stem = f"{slugify(doc_title(f['name']))[:60]}-{fid[:6]}"
        prev = old_slides.get(fid)
        entry = {
            "file_id": fid, "talk": talk, "drive_name": f["name"], "title": doc_title(f["name"]),
            "source_url": f"https://drive.google.com/file/d/{fid}/view", "file": f"{stem}.pdf",
            "format": "pdf", "mtime": f.get("mtime"),
        }
        if (prev and not force and prev.get("mtime") == f.get("mtime")
                and (prev.get("external_url") or (SLIDES_DIR / prev["file"]).exists())):
            resources[slug]["slides"].append(prev)
            report["reused"] += 1
            continue
        if dry_run:
            resources[slug]["slides"].append(entry)
            continue
        try:
            pdf, how = to_pdf(f)
            ptr = follow_pdf_pointer(pdf) if how != "bundle" else None
            if ptr and ptr[0] == "web":
                entry.pop("file"); entry.pop("format")
                entry["external_url"] = ptr[1]
                resources[slug]["slides"].append(entry)
                print(f"  LINK  {f['name'][:50]:<52} pointer -> {ptr[1]} (linked, not copied)")
                continue
            if ptr:
                pdf, how = to_pdf({"id": ptr[1], "mime": ptr[2], "name": f["name"]})
                how = f"pointer -> {ptr[1]} ({how})"
            if how == "bundle":
                entry.update({"file": f"{stem}.zip", "format": "zip"})
                pages, n_red, has_text = None, 0, True
            else:
                pdf, pages, n_red, has_text = redact_emails(pdf)
        except Exception as e:                                   # noqa: BLE001
            report["failed"].append((f["name"], str(e)[:120]))
            continue
        (SLIDES_DIR / entry["file"]).write_bytes(pdf)
        entry.update({"pages": pages, "bytes": len(pdf), "how": how,
                      "sha256": hashlib.sha256(pdf).hexdigest()[:16],
                      "emails_redacted": n_red, "needs_visual_check": not has_text})
        resources[slug]["slides"].append(entry)
        report["built"] += 1
        report["redacted"] += n_red
        if not has_text:
            report["visual_check"].append(entry["file"])
        print(f"  FILE  {f['name'][:50]:<52} {pages or 0:>3}p  {how}"
              + (f"  [{n_red} email(s) redacted]" if n_red else "")
              + ("  [NO TEXT LAYER — check by eye]" if not has_text else ""))

    # Decks published as web pages (talk-kit player) rather than Drive files.
    for talk, d in sorted(load(WEB_DECKS, {}).get("decks", {}).items()):
        if talk not in prog:
            report["failed"].append((d["title"], f"talk {talk} not in programme"))
            continue
        root = (ROOT.parent / d["root"]).resolve()
        page = root / d["path"].strip("/") / "index.html"
        if not page.exists():
            report["failed"].append((d["title"], f"no built deck at {page}"))
            continue
        slug = resource_for(talk)
        key = f"web:{talk}"
        mtime = int(page.stat().st_mtime * 1000)
        prev = old_slides.get(key)
        entry = {
            "file_id": key, "talk": talk, "drive_name": None, "title": d["title"],
            "source_url": d["live_url"], "file": f"{slugify(d['title'])[:60]}-web.pdf",
            "format": "pdf", "mtime": mtime,
        }
        if (prev and not force and prev.get("mtime") == mtime
                and (SLIDES_DIR / prev["file"]).exists()):
            resources[slug]["slides"].append(prev)
            report["reused"] += 1
            continue
        if dry_run:
            resources[slug]["slides"].append(entry)
            continue
        try:
            pdf, pages, n_red, has_text = redact_emails(export_web_deck(root, d["path"]))
        except Exception as e:                                   # noqa: BLE001
            report["failed"].append((d["title"], str(e)[:120]))
            continue
        (SLIDES_DIR / entry["file"]).write_bytes(pdf)
        entry.update({"pages": pages, "bytes": len(pdf), "how": "web deck export",
                      "sha256": hashlib.sha256(pdf).hexdigest()[:16],
                      "emails_redacted": n_red, "needs_visual_check": not has_text})
        resources[slug]["slides"].append(entry)
        report["built"] += 1
        report["redacted"] += n_red
        print(f"  WEB   {d['title'][:50]:<52} {pages:>3}p  from {d['live_url']}")

    # Transcripts, one file per video, split into the programme items it holds.
    for slug, r in resources.items():
        vid = r["video_id"]
        if not vid or not (TIMED_DIR / f"{vid}.json").exists():
            continue
        lines = caption_lines(vid)
        talks = r["talks"] or [{"talk": None, "start": 0, "end": None}]
        sections = [{
            "talk": p["talk"], "title": prog[p["talk"]]["title"] if p["talk"] in prog else None,
            "start": p["start"] or 0, "end": p["end"],
            "paragraphs": paragraphs(lines, p["start"] or 0, p["end"]),
        } for p in talks]
        r["transcript"] = f"transcripts/{vid}.json"
        if not dry_run:
            save(TRANS_DIR / f"{vid}.json", {
                "video_id": vid, "source": "YouTube automatic captions, unedited",
                "sections": sections})

    manifest = {
        "_about": "Protocol Symposium 2026 archive — built by ingest/archive_symposium.py. "
                  "One entry per protocolized.io resource page; `talks` are programme slugs "
                  "(protocol-institute.org) the page covers, with their span of the video.",
        "event": EVENT_NAME,
        "programme_url": BASE_URL + EVENT_PATH + "/",
        "resources": dict(sorted(resources.items())),
        "talk_resource": dict(sorted(talk_res.items())),
    }
    if not dry_run:
        save(MANIFEST, manifest)

    n_new = sum(r["new"] for r in resources.values())
    print(f"\n── Summary ──  resources {len(resources)} ({n_new} new)   talks linked {len(talk_res)}"
          f"   PDFs built {report['built']}  reused {report['reused']}"
          f"   emails redacted {report['redacted']}   transcripts "
          f"{sum(1 for r in resources.values() if r['transcript'])}")
    for name, why in report["failed"]:
        print(f"  FAIL  {name[:50]:<52} {why}")
    for name in report["visual_check"]:
        print(f"  LOOK  {name}  — no text layer; check for contact details by eye")
    return manifest


def website_data(manifest: dict) -> dict:
    """What the protocol-institute.org programme page needs: per programme slug,
    where its canonical page is and what that page carries."""
    out = {}
    for talk, slug in manifest["talk_resource"].items():
        r = manifest["resources"][slug]
        out[talk] = {
            "resource_url": f"{PROTOCOLIZED}/resources/{slug}",
            "slides": sum(1 for s in r["slides"] if s["talk"] == talk),
            "transcript": bool(r["transcript"]),
        }
    return {
        "_about": "Where each Protocol Symposium 2026 item lives in the protocolized.io "
                  "archive. Written by c3po (ingest/archive_symposium.py --website); "
                  "do not hand-edit.",
        "talks": out,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="rebuild every PDF")
    ap.add_argument("--protocolized", default=str(ROOT.parent / "protocolized-website"),
                    help="protocolized-website checkout (for existing resource slugs)")
    ap.add_argument("--website", help="website checkout: also write " + WEBSITE_DATA)
    a = ap.parse_args()
    m = build(Path(a.protocolized), dry_run=a.dry_run, force=a.force)
    if a.website and not a.dry_run:
        save(Path(a.website) / WEBSITE_DATA, website_data(m))
        print(f"  wrote {Path(a.website) / WEBSITE_DATA}")
