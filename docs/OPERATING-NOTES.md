> Internal operating notes — the working reference for running this day to day.
> Start with the [README](../README.md).

# strategy — the strategic layer as data (sea-mii7)

**Lives here:** `~/projects/strategy` (yours may differ) (own package, five light deps). **Installed as a
tool:** `uv tool install --python 3.12 ~/projects/strategy` → `~/.local/bin/strategy`; after a change,
`uv tool install --reinstall --python 3.12 ~/projects/strategy`. `uv tool install` does not read
the repo's `.python-version`, so say 3.12 or the tool lands on whatever uv considers newest
(it was on 3.14 until 2026-09-26). Promoted out of the SEA repo on
2026-08-17 so the session hook depends on nothing but an installed binary.
**Schema still evolves with SEA:** `sea do "…" --models ~/projects/strategy/src/strategy_store/models.py`.
**Own rig since 2026-09-03:** `~/gt/strategy` (bead prefix `st`, `dc strategy`). The
store's beads moved out of the sea rig and KEPT their `sea-` ids (`bd import` does not
re-prefix), so every `sea-mii7*` citation in this repo still resolves — look for them in
the strategy rig, not in sea. `sea-70a3` is a genuine SEA bead and stayed there.

Rigs make local decisions; nothing above them says which objective they serve.
This store holds **positions** (strategic claims) with a status, a *declared*
scope, a framework slot, and a supersession chain — lifted out of the places
they already live: Vikunja title prefixes (`DECIDED:` / `CONFLICT:` / …) and
`type: decision` beads.

```
strategy init            # STRATEGY_DB_URL (env or ~/.env) — since 2026-08-20 the followwhee schema on factumerit-db; else SQLite at ~/.strategy/store.db
strategy seed            # the projects in STRATEGY_VIKUNJA_PROJECTS, plus decision beads across rigs
strategy list            # current positions (superseded hidden; --all to show)
strategy show 22
strategy set 22 --status resolved --element ptw.box2
strategy supersede 30 19 [--evidence "..."]   # scope rule enforced
strategy reconcile       # source vs store drift — reports, never rewrites
strategy export          # JSONL → $STRATEGY_EXPORT_PATH's folder/{positions,drops,metrics}.jsonl (private repo = the backup)
strategy seed-forcing    # the hand-collected dated gates (forcing_seed.py, cites sources)
strategy forcing         # gates soonest first; undated last (they are the finding)
strategy add --title … --scope … --source …   # a position by hand, scope declared
strategy report --write  # Playing_to_Win_Report.md — the cascade AS OUTPUT (sea-mii7.1)
strategy prime [--rig R] [--seed-if-older-than 24]   # the Direction block for session start
strategy drop log --url … [--format article|post|carousel|video|comment-link] [--app URL] [--own-comments N] [--own-reposts N]
                                  # re-run on a URL to update fields; my own comments/reposts never count as engagement
strategy drop stats ID [--impressions N] [--reactions N] [--comments N] [--reposts N] [--clicks N] [--follows N] [--as-of D] [--source …]
                                  # hand entry for LinkedIn PAGE posts (no single-post export; numbers only in the page admin UI)
strategy drop ingest --downloads /path/to/Downloads --archive   # LinkedIn per-post xlsx exports;
                                  # --archive moves them to drops/exports/ (raw per-day record)
strategy drop list [--days 30]     # scoreboard: on base? + DOWNLOAD STATS nags
strategy drop backfill-ids         # one-time: fill activity_id/share_id from URLs + archived exports
strategy drop funnel [--id N]      # LinkedIn clicks → landing views → tables → hands (page_views plane)
strategy metric add --name N --beat B --source buttondown-api|linkedin-export|manual [--source-key K] [--gates 300,600,1000] [--due D] [--position 49]
strategy metric read N VALUE [--on D]   # a hand reading (one per metric per day; re-run to correct)
strategy metric pull               # Buttondown counts via API (tokens from env / ~/.env / qrcards .env; cron 18:10 daily)
strategy metric list [--readings]  # latest, change since, next gate — `prime` prints the goal lines
```

**Getting on base.** A drop is on base if it gained ≥1 follower, or got ≥1 comment (not mine — `--own-comments`, default 1 for comment-link) or repost (not mine — `--own-reposts`; my main-feed share of a page post is a repost of my own post), or
sent ≥5 people to the app. LinkedIn only gives per-post stats as a hand export (post → View
analytics → Export), so `prime` says which drops need one — 2 days after posting, and again
once the post has settled (7 days) if the first export was early.

**One post, two ids.** A LinkedIn post URL carries either its activity id (`…-activity-<id>-…`,
the browser URL and the export's filename) or its share id (`…-share-<id>-…` / `…-ugcPost-<id>-…`,
the export's Post URL cell); the slug text differs too. `drop log` and `drop ingest` match on
either id before falling back to the URL, so a post hand-logged by one URL form and exported under
the other stays one drop (the first-logged URL is kept). `drop backfill-ids` fills the ids on
older rows.

`prime` is appended to every session by `~/.local/bin/claude-hook-prime` (user-level hook)
(wired in `~/.claude/settings.json` SessionStart + PreCompact, so it fires in every rig, not only the mayor): `bd prime`, then
`strategy prime --seed-if-older-than 24` for the rig detected from cwd — 0.3s when cached,
~3.5s on the once-daily reseed, hard 45s timeout, exit 0 always, prints nothing extra if
`strategy` is not installed. Push, not pull — an agent that does not know the store exists
never queries it.

`forcing` was scaffolded with `sea do "add forcing table with …"` (add_table_with_fk):
SEA placed and spliced the class cleanly, but typed every column String(255) nullable
and did not wire the FK — hand-corrected in models.py; the gap is filed on sea-mii7.

Rules: sources own text; the store owns status / scope / element / supersedes.
A narrower-scope row may not supersede a wider one unless it names new evidence.
`element_key` NULL is a finding (orphaned position), not a default to fill.

Evolve the schema with `sea do … --models src/strategy_store/models.py`; the
deferred tables (venture, objective, forcing, constraint, framework/element/
test/assessment) and the report (sea-mii7.1) go on top of this.

## Working protocol

**Nothing to open.** `strategy prime` is appended to every session by the user-level hook, so
the Direction block — aspiration, gates, goals, the drop scoreboard — is already in context
before the first question. Push, not pull: an agent that does not know the store exists never
queries it.

**The store is Postgres** (`STRATEGY_DB_URL`), and the CLI is its only writer. The SQLite file
under `~/.strategy` is a fallback, not the truth. Never hand-edit either.

**Where things live.** Three homes, and confusing them has already cost real time:

| what | where |
|---|---|
| the store | Postgres via `STRATEGY_DB_URL` (env or `~/.env`) |
| the CLI source — features, schema, this file | this repo |
| prose, exports, the generated memo, LinkedIn per-post exports | the tracked working directory |
| private config: `store.db`, `forcing-seed.yaml`, `seed-map.yaml` | `~/.strategy`, deliberately outside git |

The working directory must be a TRACKED one. It was not, once: `report.py` read
`memo_prose.yaml` from a path that its own repo gitignored, so the hand-written paragraph
heading the memo had no history. It went stale for six days — announcing a submission that had
landed and a decision that had been made — while the generated numbers beneath it refreshed on
every run. Nobody could see the drift, because the file was invisible to git. Fixed 2026-08-23;
do not point a read at an ignored path again.

**Recording a decision.** `strategy add --title … --scope … --source …`, then
`strategy set <id> --status resolved` once it is blessed. **Never delete a position** and never
write a self-voiding clause into one — `strategy supersede <new> <old>` is the mechanism, and it
enforces the scope rule. A position that stops describing the work is replaced, not revoked.

**Retiring a met gate.** Put `resolved_on:` on its row in the forcing seed and run
`strategy seed-forcing`. The row stays in the file as a record of the commitment and drops out
of `forcing`, `report` and `prime`, all of which already skip resolved gates.

**The prose is the one thing that can lie.** Everything in the memo regenerates from the store
except the paragraph at the top and the per-box paragraphs, which are hand-written in
`memo_prose.yaml`. When a gate closes, fix the prose in the same sitting — the numbers will
correct themselves and the sentences will not.

**Drift is information.** `strategy reconcile` reports and never rewrites. A row sitting
`holding` while its source bead closed is a deliberate signal that the source's conclusion has
not been accepted yet. Do not auto-flip it; ask.

**After any code change here:** `uv tool install --force <this repo>`, or the installed binary
keeps running the old code. A gate that would not retire, and a seed field that appeared to do
nothing, were both this.

**Adding features.** Code in this repo; schema through
`sea do "…" --models src/strategy_store/models.py`; the work is tracked on the **sea** rig under
`sea-mii7`. The data home holds no code and this repo holds no private rows — the seed files are
loaded at runtime from outside it, which is what makes the repo publishable.
