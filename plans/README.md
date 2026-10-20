# Plans — index and current state

Reviewed 2026-10-19 (session 62) against the code, `status.md` and the devlog. Each plan's own
status banner was corrected to match; history below the banner is kept as written. Plans stay at
their paths (other projects, the devlog and `meta` vectors link to them), so "done" means
reference, not deleted. **Open work is tracked in `status.md` → *Open items*, not here.**

## Active

| Plan | State | Next |
|---|---|---|
| [research-group-highlights.md](research-group-highlights.md) | Exporter built; MRG archive with the MRG host for review. Rename to "Research Groups" decided | Host feedback, then all groups, password-protected download, weekly refresh |
| [reranker.md](reranker.md) | Phases 0-2 shipped (rerank-3 on every answer path). Phase 3 data collection live since 2026-10-10 | Phase 3 analysis at ~100-150 real questions (`bin/pull_query_log.py --stats`) |
| [event-awareness.md](event-awareness.md) | Phases A, B, C, E, F shipped (session 58) | Phase D: `event_id` for Discord, Substack, PDFs |
| [vm-credential-hardening.md](vm-credential-hardening.md) | Phase 1 done 2026-09-09 (scoped PATs) | Phase 2: services off `exedev`/sudo; phase 3: standing assertions |
| [pinecone-quota-management.md](pinecone-quota-management.md) | Living doc: c3po's status against the PI-wide quota SOP | Update when quota behaviour changes |
| [answer-path-playbook.md](answer-path-playbook.md) | Reference for the other RAG bots; adoption tracked in `Code/devops/status.md` | Update its per-bot table as bots adopt items |

## Approved, not started

| Plan | State |
|---|---|
| [website-interface.md](website-interface.md) | c3po should write JSON only; `generate_sig_pages.py`/`update_sig_pages.py` still write HTML into the website |

## Backlog (dormant; revive deliberately)

| Plan | State |
|---|---|
| [structural-navigation.md](structural-navigation.md) | Exhibit extraction (figures, tables, enumerated lists) from PDFs. Not built |
| [magazine-lexicon.md](magazine-lexicon.md) | Lexicon pass over Protocolized magazine. Not run: the 914-term lexicon is from PDFs only |
| [sig-meeting-capture.md](sig-meeting-capture.md) | Structured meeting-capture protocol for RG hosts. Waiting on a host discussion; partly overtaken by the #meeting-notes audio ingest |
| [roam-ingest.md](roam-ingest.md) | SIGFPT Roam graph export (`c3po_inbox/`). Not built; sig-meeting-capture argues against Roam as a source |
| [bot-ecology.md](bot-ecology.md) | Phases A-C done; D partly (`config/bot_registry.json`, monitoring page); E (swarm: more gateways, orchestrator) future |
| [vgrzirp-reuse.md](vgrzirp-reuse.md) | Partly adopted (tier weights, corpus map, lexicon injection, MCP, search, strike tracking). Not adopted: title-anchored embeddings, D1 transcripts/query log, transcript encryption |

## Done (kept for reference)

| Plan | Outcome |
|---|---|
| [symposium-ingest.md](symposium-ingest.md) | Phases A-C built; archive on protocolized.io. Ongoing only as late uploads arrive |
| [rename.md](rename.md) | c3po → PIBot executed session 58 (2026-10-07–09) |
| [discord-awareness.md](discord-awareness.md) | Phases 1-4 done (Phase 4 nav queries: session 26) |
| [discord-guide-scope.md](discord-guide-scope.md) | `should_embed()` scope policy implemented in `sync_discord_channels.py` |
| [meeting-notes-ingest.md](meeting-notes-ingest.md) | `sync_meeting_notes.py` live in the daemon |
| [oracle-bot-setup.md](oracle-bot-setup.md) | Discord Interactions oracle (`/interactions` + queue) deployed |
| [resource-pipeline.md](resource-pipeline.md) | YouTube and PDF enrichment both flow to protocolized-website via daemon steps |
| [youtube-ingest.md](youtube-ingest.md) | `videos` namespace live (97 talks) |
| [bibliography-mining.md](bibliography-mining.md) | `bibliography` namespace live |
| [exe-dev-migration.md](exe-dev-migration.md) | Executed 2026-08-01; daemon + bot on `c3po-vm.exe.xyz` |
| [vps-migration.md](vps-migration.md) | Superseded by exe-dev-migration; never executed |
