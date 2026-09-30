# strategy

**The strategic layer as data.** Positions you actually hold, the gates that force
them, and the evidence underneath — in one store you can query, instead of
scattered across task titles, issue trackers, and documents nobody re-reads.

MIT licensed. Runs entirely on your machine, against SQLite or Postgres.

## The problem it solves

Teams and solo operators make strategic decisions constantly, and then lose them.
The decision lives in a task title, a closed issue, a doc from March. Six weeks
later the same question gets re-argued from scratch, or worse — two parts of the
work quietly proceed on contradictory answers.

Meanwhile the backlog grows. And you cannot decide your way out of a backlog by
ranking it, because ranking is per-item and the list is infinite.

**A strategy is a filter.** Once you can state the positions you hold, the work
sorts itself: these survive, those don't. Change the position and the survivors
change with it. That is the operation this store exists to make cheap — and it
is why the answer to an overflowing list is upstream of the list.

## What it holds

| | |
|---|---|
| **Position** | A strategic claim, with a status (`holding` / `resolved` / `contested` / `retired`), a *declared* scope, a framework slot, and a supersession chain. |
| **Forcing gate** | A dated condition that forces a decision, and — the important field — **what it gates**. That is what turns a list of dates into a dependency graph. A gate with no date is itself the finding: deferral without a trigger. |
| **Evidence** | An observation attached to a position, so a claim can be checked against what actually happened rather than re-argued. |
| **Metric / drop** | The numbers being chased over time, and whether what you published moved them. |

Positions are **lifted from where they already live** rather than typed twice —
task-title prefixes (`DECIDED:`, `CONFLICT:`, `WATCH:`) and `type: decision`
issues. Sources own the text; the store owns status, scope, and supersession.
Re-running the sync refreshes text and *reports* drift, never silently rewriting
your judgment.

## Quick start

```bash
uv tool install .            # → strategy
strategy init                # SQLite at ~/.strategy/store.db, or set STRATEGY_DB_URL
strategy add --title "Sell to teams, not individuals" --scope org --source manual
strategy list
strategy forcing             # dated gates, soonest first; undated last
strategy report              # the cascade, rendered from the store
strategy claim 54 30         # two systems, one question: render it as ONE dispute
strategy assess purr t6 --verdict fail --note "needs Ivan in the room"
strategy prime               # a compact Direction block for the start of a work session
strategy doctor              # which store am I on, where did each setting come from, how stale
```

`strategy --help` lists everything. Fuller day-to-day notes are in
[docs/OPERATING-NOTES.md](docs/OPERATING-NOTES.md).

## Configuration

Everything is environment variables; nothing personal is compiled into the code.

| Variable | Purpose | Default |
|---|---|---|
| `STRATEGY_DB_URL` | Where the store lives | SQLite at `~/.strategy/store.db` |
| `STRATEGY_EXPORT_PATH` | JSONL backup target | `~/.strategy/positions.jsonl` |
| `STRATEGY_SEED_MAP` | Framework-slot map + stated supersessions | `~/.strategy/seed-map.yaml` |
| `STRATEGY_FORCING_SEED` | Hand-collected gate rows | `~/.strategy/forcing-seed.yaml` |
| `STRATEGY_VIKUNJA_PROJECTS` | Vikunja projects to sync, as `id:Name,id:Name` | none |
| `STRATEGY_DOWNLOADS_DIR` | Where hand-exported LinkedIn stats land | none |
| `GT_ROOT` | Root of the rig checkouts to read beads from | `~/gt` |

Every one of these is read the same way, in one place
([`env.py`](src/strategy_store/env.py)): the process environment first, then
`~/.env` (and `~/gt/qrcards/crew/ivan/.env`), then the default — with `$HOME`
expanded the way a shell would. `strategy doctor` prints each value and which of
those three supplied it. Two resolution paths for one setting is how a bare
shell and a `. ~/.env` shell became different programs, reading different data
and saying nothing about it (sea-mii7.3).

Those files are read with python-dotenv, so `${SHARED_PG_HOST}`-style references
resolve the way they would in a shell that sourced them — `~/.env` composes its
connection strings from parts so that rotating a role edits one line, and
`os.path.expandvars` resolved none of it, which took the store offline for every
shell that had not sourced the file (hq-f8za). Two things follow:

- **A reference nothing defines drops the key**, with one line on stderr and in
  `strategy doctor` naming the key, the file and the reference. It does not come
  back as a URL with an empty host, which is what dotenv alone would return and
  what would then quietly dial the local socket.
- **Quoting does not protect `${`.** python-dotenv interpolates inside single
  quotes as well as double, and `\${` is *not* honoured as an escape — the
  backslash is dropped and the reference is eaten anyway. There is no way to
  write a literal `${` in these files. Put such a value in the process
  environment instead (`export VAR=...` in the shell), which wins over both
  files. A test pins this, so a future python-dotenv release that changes it
  fails the suite rather than this paragraph going quietly wrong.

If `STRATEGY_DB_URL` is set and cannot be opened, the CLI fails and says so. It
never quietly falls back to the local SQLite copy: an answer you cannot tell
from a live one is the failure this store exists to surface.

**Your positions and gates are deliberately not in this repository.** They name
real commitments, real dates, and real people. The store reads them at runtime
from files you keep wherever you keep private things. See
[`examples/`](examples/) for the shapes, with synthetic rows.

## The admin surface

`strategy web` serves the CLI's read side as browsable pages — the cascade by
framework box, positions with their evidence and supersession chains, forcing
gates (undated ones called out, not merely listed last), the pending pile,
source-vs-store drift, the tests × ventures matrix, metrics, drops, the funnel,
the profile check, and the generated memo — and `/triage`, which walks every open
issue one card at a time and takes the decision.

```bash
uv sync --extra web
strategy web                  # http://127.0.0.1:8021
```

It is an **admin surface**, and the distinction is deliberate: this is the
rawest form of the store that is still honest — every row, every source ref,
every declared scope, nothing staged or curated. Features earn a curated home
elsewhere *after* they have a shape here.

**Writing is confined to `/triage`,** and a test asserts that every other route
is a read. The invariants that protect judgment live in [`core.py`](src/strategy_store/core.py)
— supersession refused when the newer row saw less than the row it replaces, a
sync that never overwrites store-owned columns, a rejection that must carry a
reason — and both surfaces *call* them rather than re-implementing them. The
test for that is behavioural: rejecting from the web without a reason is
refused, and that rule exists in exactly one place, so the refusal is evidence
the route went through `core`.

The surface binds to loopback and has no login, which is fine while it reads.
Because it now writes, a cross-origin form POST is refused — browsers set
`Origin` on form posts, so requiring it to match closes that door without adding
a session to a single-user tool.

The read logic is shared the same way: `queries.py` and `report.build_context`
are what both the CLI and the web surface call, so `strategy forcing` and
`/gates` cannot answer differently, and `/triage` and `strategy triage` cannot
show a different case for the same position — both render `queries.dossier`.

`/commands` is the coverage map — every CLI command, whether it writes, and
which page shows it. The list is walked out of the click group at runtime, so a
command added to the CLI cannot quietly go missing from it; two tests fail if a
new command is unclassified or if a read command has no page.

Nothing personal lives in the package: it renders whatever `STRATEGY_DB_URL`
resolves to, and `/store` (like `strategy doctor`) says which store that is.

## Public surfaces

`strategy profile` holds what a public surface (LinkedIn first) currently says,
with every factual claim pointing at what licenses it — a position, a piece of
evidence, a drop, a metric reading, or a registered app.

```bash
strategy profile set headline --text "…"
strategy profile claim headline --text "182 ms across 12 phones" --license position:20
strategy profile check        # exits non-zero when something needs fixing
strategy profile export       # surface order, character counts, ready to paste
```

`check` compares the copy against the store: claims with nothing behind them,
copy advertising a segment a position declined, cadence words the drop count does
not support, memo shorthand in public wording, claims licensed by a decision that
has since been replaced, and products named that the app registry does not list.
Two of those need the private rules file (`STRATEGY_PROFILE_RULES`, shape in
[`examples/profile-rules.yaml`](examples/profile-rules.yaml)) — without it they
report "could not run" rather than passing, because a check that cannot see its
rules has not cleared anything.

## Reading it from a chat (MCP)

`mcp-server/` is generated — `sea crab generate --models src/strategy_store/models.py
-o mcp-server --name strategy --readonly --dsn-env STRATEGY_DB_URL` — so a Claude
Desktop conversation can query positions, gates, evidence, assessments, drops and
metrics. Regenerate it after a schema change; do not hand-edit it.

It is **read-only on purpose**. Positions carry judgment, and the invariants that
protect them live in the CLI: supersession is refused when the newer row saw less
than the row it replaces, a sync never overwrites store-owned columns, and a claim
is contested when its members disagree. A generic `update_row` on `positions` would
walk past all three. Writes stay where the rules are.

The launcher sources `~/.env` (or `$CRAB_ENV_FILE`) and maps `STRATEGY_DB_URL` into
the server, so the connection string is never copied into the Desktop config.

## Why the data is separate

A strategy store is only useful if it is honest, and it is only honest if you can
write things in it that you would not publish. Keeping the rows outside the
package means the tool can be open source without asking you to be.

The same goes for the code's own history. Commit messages and fixtures tend to
quote the store as evidence: a task id, a gate's text, someone's name. So this
repo is published as snapshots. `scripts/publish_snapshot.sh` pushes HEAD's tree
as one commit, and only after `scripts/check_publishable.py` has found no real
task ids, patent numbers, or terms from a private list kept outside the repo
(`STRATEGY_PRIVATE_TERMS`). `tests/test_publishable.py` runs the same check in the
suite.

## Design notes

- [Two writers, one row](docs/two-writers-one-row.md) — who owns which column,
  why some edits are refused, and the difference between a clobber and a drift.
- [Operating notes](docs/OPERATING-NOTES.md) — the day-to-day working reference.

## License

MIT — see [LICENSE](LICENSE).
## Python version

Pinned to **Python 3.12** by the git-tracked `.python-version` at the repo root.
`git worktree add` checks that file out, so every worktree matches the deploy
interpreter instead of resolving to whatever uv considers newest on the box.

Don't delete it, and don't create a venv with an explicit `--python` that
disagrees with it. Unpinned trees in this town drifted to 3.14, and a 3.13
stdlib change (`Path.exists()` swallowing `PermissionError`) made a security
boundary return 503 in one tree and 200 in another on the same commit — hq-nxkv.
