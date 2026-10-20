# Research Group highlights archive + the "Research Groups" rename (draft for VGR review)

Drafted 2026-10-19 (session 62) from VGR's ask: *compile a comprehensive, accumulating archive of
noteworthy posts for every SIG channel, accessible via an API call or MCP; and SIGs are known as
Research Groups going forward.*

## 0. Status (2026-10-19): simpler first step chosen

VGR chose a plain weekly archive file per group before any curation or API: `ingest/export_rg_archive.py`
(JSON, gitignored `data/rg_archives/`), to be served to RG hosts behind a password on the Worker.
MRG built first and sent to the MRG host for review. Sections 2a-2b below are the later, optional
curated layer. Open TODOs are in `status.md` (session 62).

## 1. What the ingest does today (verified 2026-10-19)

The daemon scans every targeted channel each cycle (~30 min): general channels -> `discord`,
the 8 Research Group channels -> `sig`, URLs found in either -> `discord_links`.

**In Research Group channels it captures far more than meeting threads.** Sampled counts in the
`sig` namespace (all groups):

| chunk_type | vectors | what |
|---|---|---|
| `sig_message` | ~5,090 | main-channel original posts (>= 20 chars, or URL, or starred) |
| `sig_reply` | ~1,110 | replies (>= 150 chars, or URL, or starred) |
| `sig_discussion` | ~295 | non-meeting threads, bundled one chunk per thread |
| `sig_attachment` | ~30 | PDFs etc. attached to posts |
| meeting family | ~1,300 | `sig_meeting_summary/body/page`, `audio_meeting_summary/section` |

**But nothing today is a "noteworthy" judgment.** The filters above are inclusion thresholds, not
curation: everything over a low bar is embedded on equal footing. The only relevance scoring is
on *shared links* (`enrich_discord_links.py`, Haiku 0-3), not on posts. Star reactions are the
one curation signal and almost nobody uses them: 17 starred posts across all eight groups.

**Access today is query-only.** `GET /search?q=` and the MCP tool `search_corpus` (namespace
`sig`) return semantic matches to a question. There is no way to list "what was notable in MRG
in September", no per-group feed, no browse-by-date, and no curated layer. So the honest answer
to "do we compile noteworthy posts?" is: we collect, we do not compile.

## 2. Proposed: a curated, accumulating highlights archive per group

Principle (from `website-interface.md`): c3po produces JSON; renderers (website, MCP clients) own
presentation.

### 2a. Curation step: `ingest/curate_rg_highlights.py` (daemon, every cycle)

- Input: new `sig_message` / `sig_reply` / `sig_discussion` chunks per channel since its own
  cursor (`data/rg_highlights_state.json`), batched per channel per cycle. Meeting threads are
  out of scope: they already have the meeting archive on .org and the `sig_meeting_*` vectors.
- One Sonnet call per channel batch (memory rule: Sonnet, not Haiku; the material is dense).
  Context given: the group's description and blurb from `config/discord_channels.json`, the
  batch of posts with author, timestamp, reply count, star count, URLs, and for URLs the
  `discord_links` relevance score if already present.
- Output per post: `score` 0-3 (0 chatter / scheduling, 1 ordinary, 2 substantive, 3 landmark:
  a new idea, a key reading, a decision, a result), `gist` (one line), `tags` (<= 4), `reason`.
  Keep score >= 2. Threshold tunable; store all scores in the state so it can be re-cut without
  a re-run.
- Write-back: `highlight_score` + `gist` on the Pinecone vector metadata (`idx.update`), so the
  answer path can later weight highlights up (not in v1).
- Archive: `data/rg_highlights/<slug>.json`, append-only, one record per kept post:
  `{message_id, channel_id, group, author, ts, ts_unix, text, urls, star_count, reply_count,
  score, gist, tags, discord_url, thread_id?}`. Committed by the daemon's routine state sync, so
  the archive accumulates in git and survives a fresh clone (lesson from the exe.dev migration).
- Backfill: `--backfill` over the existing ~6,500 posts once. Estimate: a few dollars of Sonnet,
  one-off. Steady state: ~100-150 new posts/week across all groups, well under $2/week.

### 2b. Serving: KV + Worker routes + MCP tool

- Push: after each run, `PUT /api/admin/highlights` (admin key, like `/api/admin/events`) with
  the per-group archives; Worker stores `highlights:<slug>` and `highlights:index` in KV. No
  Pinecone egress on reads.
- `GET /api/highlights` -> index: groups, counts, last post, last run.
- `GET /api/highlights/<slug>?since=YYYY-MM-DD&until=&min_score=2&limit=50&cursor=` -> records,
  newest first. Slugs = the channel manifest names (`formal-protocol-theory`, ...).
- MCP tool `research_group_highlights` (`group`, `since`, `until`, `min_score`, `limit`), listed
  next to `search_corpus`; `search_corpus` description gains the `highlight` fields.
- Optional later: `?format=md` for a paste-ready digest; the website renders a per-group
  "Highlights" tab from the same JSON (website owns rendering).

### 2c. Decisions needed from VGR before building

1. **Exposure.** Posts are already reachable through `/search` and `search_corpus` with author
   usernames, so the archive adds no new *data*, but a browsable curated feed is more exposure
   than a search hit. Public like `search_corpus`, or gated behind `MCP_API_KEY`? Recommend:
   public, authors by Discord display name only, opt-out by reacting with an agreed emoji.
2. **Threshold and taste.** Score >= 2 keeps roughly the top third (guess; calibrate on the
   backfill of one group, review by eye, then run the rest).
3. **Include `general`-type channels later?** Same machinery works for `discord`; v1 is
   Research Groups only, as asked.

## 3. The rename: SIGs -> Research Groups

Same principle as `rename.md`: rename what people see, alias what they call, leave the plumbing.

**Already done (session 59, 2026-10-10):** the website moved `/sigs/` -> `/research-groups/`;
generators, crawl paths and `WEBSITE_PATHS` follow; vector ids stay keyed on the old URL.

**Tier 1, wording people see (to do):**
- `api/worker.js`: ~60 strings say "SIG", "SIG groups" or "Special Interest Group": persona
  `SYSTEM_PROMPT`/`SOUL_EXCERPT`, the corpus map line (~1618, also still says `/sigs/`), web
  intro, Corpus Status and How It Works tables, the status page "SIG Meeting Archive", both MCP
  tool descriptions, the `/search` namespace help. One sweep, one deploy.
- `ingest/generate_sig_pages.py` SIG_INFO names ("SIGPSY — Special Interest Group in
  Psychohistory"); `sync_meeting_notes.py` SIG_TITLE_MAP values; `bin/c3po_bot.py` copy.
- `config/discord_channels.json`: Haiku blurbs/descriptions say "SPECIAL INTEREST GROUPS"
  because that is the Discord category name. **VGR renames the Discord category**; then
  `sync_discord_channels.py --refresh` regenerates the affected blurbs.
- New vectors: `sig_meeting_summary` headers say "SIG: <display>"; switch to "Research Group:".
  Old vectors keep the old wording (history), as with the PIBot rename.

**Tier 2, names people call:** MCP namespace enum value `sig` stays (third-party configs);
the description text changes to "Research Group archives".

**Tier 3, plumbing (keep):** namespace `sig`, `chunk_type` `sig_*`, metadata key `sig_display`,
`sync_sig.py`, `sig_state.json`, `data/sigs/`, `sigs:` event-series keys. Not user-visible.

**Open question for VGR:** the group acronyms themselves (SIGFPT, SIGPfB, SIGPSY, ProtFiSIG)
carry "SIG". They are the groups' own names and appear in thread titles, event names and
the `sig_display` metadata the answer path filters on. Recommend leaving the acronyms as the
groups themselves use them, and changing only the category word; if a group renames itself
(e.g. FPT), add the new name as a display alias rather than rewriting history (memory:
normalising labels stripped meaning once already).
