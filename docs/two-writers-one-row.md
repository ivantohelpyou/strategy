> Explainer — why some edits in this store are refused, and why one of them
> used to be silently undone. Start with the [README](../README.md).

# Two writers, one row

Every fact in this store has an owner: the system allowed to write it.
Some facts arrive from outside — a position's title comes from Vikunja or
from a bead, a gate's condition comes from the forcing-seed file. Some are
made here and exist nowhere else — a position's status, its evidence rows,
the note explaining why a gate was closed.

The rule is **one writer per fact**. Everyone else reads.

It sounds obvious. It is broken by accident every time a column looks
convenient, and the resulting failure is quiet, which is what makes it worth
an explainer.

## What going wrong looks like

Not an error. That is the whole problem.

Two writers sharing a column do not collide — the second one wins, and the
first one's work is simply gone the next time the second one runs. Nothing
raises. Nothing logs. The command that lost the data reports success,
because from its point of view it did exactly what it was asked.

You find out weeks later, when something you decided is somehow undecided.

## The three cases in this codebase

They are not the same failure, and they do not have the same fix. Telling
them apart is most of the value here.

### 1. Position text — solved, and the model for the rest

A position seeded from Vikunja or beads gets its title and statement back
from that source on every `strategy seed`. The store owns its status, scope,
element and venture; upstream owns the words.

So `core.set_fields` refuses a text edit on a synced position, and says why:

> `title: text belongs to vikunja:#1234 and would be overwritten on the next
> sync — edit it there, or record the change as evidence or a factor`

Note the shape of the refusal. It does not just block; it names the real
owner and offers the two legitimate alternatives. **An edit that silently
disappears is worse than a refusal** — that sentence is the whole design.

### 2. Forcing gates — the same bug, found late (2026-09-02)

The `forcing` table is written by two things:

- **the seed file** (`~/.strategy/forcing-seed.yaml`), which owns each gate's
  title, condition, due date and "what it gates" text; and
- **the store**, via `gate resolve` — which sets `resolved_on` and *appends*
  the reason to the `gates` column.

Both wrote `gates`. Both wrote `resolved_on`. `upsert_forcing` did this:

```python
row.title, row.condition, row.due_on, row.gates, row.venture = (...)
row.resolved_on = resolved      # ← unconditional, straight from the file
```

The seed file is written by hand and knows nothing about `gate resolve`. Any
gate closed after the file was last edited had its resolution recorded **only
in the store** — so the next `strategy seed` would have set `resolved_on`
back to `None` and overwritten `gates`.

Concretely, on 2026-09-02: the file was last written 2026-08-23, and six
seed-backed gates had been resolved by hand since (#3, #4, #5, #6, #10, #11).
A reseed would have **reopened all six and deleted the notes explaining why
they closed.** Gates #1 and #2 were safe only because their resolutions
happened to have been written back into the file by hand.

Nobody would have seen a stack trace. Six settled questions would just have
been asking again, with no record of having been answered.

The fix splits the column's ownership instead of the column:

```python
kept = _store_suffix(row.gates)          # the store's appended [resolved …] / [edited …] notes
row.title, row.condition, row.due_on, row.venture = (title, cond, due, venture)
row.gates = (gates or "") + kept         # file text, then store history
if resolved:                             # only ever SETS
    row.resolved_on = resolved
```

Two rules worth stating on their own:

- **`resolved_on` is only ever set from the file, never cleared.** A file
  with nothing to say about a gate must not reopen what the store closed.
  Reopening is `gate reopen` — by hand, on purpose, and visible.
- **Store-appended history survives a reseed.** `_store_suffix` finds the
  first `[resolved …]` or `[edited …]` marker and re-attaches everything from
  there, so refreshing the file's text cannot take the log with it.

### 3. Gates versus beads — a different failure, still open

The third case has **no writer conflict at all**, which is why it went
unnoticed longest.

A gate says work is pending. A bead in some rig says that work shipped. No
system overwrites the other; they simply drift apart, because nothing ever
reads one on behalf of the other. `Forcing.source_ref` has carried bead ids
by convention for months (`beads:purr-u8s.29`) and nothing parses them.

Observed: gate #6 gated a deliverable whose bead had been closed
"SHIPPED + LIVE-VERIFIED" **eight weeks earlier**. The gate was not wrong
when it was written. Nothing had gone wrong since. It was just never asked.

### 3b. The seed file's dates — the same column, the half that was missed (2026-09-03)

Case 2 fixed `gates` and `resolved_on` and left `due_on` alone, because the file
genuinely does own the date:

```python
row.title, row.condition, row.due_on, row.venture = (title, cond, due, venture)
```

That is the design. The failure is that it was stated nowhere a reader would
meet it, so the two sides could disagree and only the next reseed would find
out. On 2026-09-03 gate #8 held `2026-12-15` in the store and `due_on: null` in
the file — so `strategy seed-forcing` would have turned a dated gate into an
**undated** one. Not moved: erased. An undated gate drops out of every count of
what is coming, so the deadline would not have slipped, it would have vanished.

This store exists to catch a date that slides with no reason on the record. It
was about to do that to itself.

Note which fix applies. `gate edit` already refuses to move a date without
`--note`, and refuses seed-owned gates outright — but **a refusal only guards
the door the store controls.** The file is the other door, reached with a text
editor, and no code of ours runs when it changes. So this one is a reconciler,
not a refusal: `forcing_seed.reseed_drift()` reports every live gate whose date
the next reseed would rewrite, separating a MOVE from a CLEAR, and naming the
file as the place to fix it rather than the store. It surfaces in `strategy
reconcile` and inline in `strategy forcing`, and it runs even under
`--no-gates`, because it costs nothing.

The general rule this case adds: **when one writer is a human with a text
editor, the refusal has nowhere to live.** Ownership rules work between
programs. Between a program and a file somebody hand-edits, only a reconciler
does.

### 4. The schema itself — the same drift, one level up (2026-09-03)

The first three cases are about a row. This one is about the table.

`follow-whee` cannot pip-install a private repo, so it carries its own
SQLAlchemy declaration of six of this store's tables — `positions`, `evidence`,
`forcing`, `drops`, `metrics`, `metric_readings` — and writes the same Postgres
schema. The rule was stated in a docstring: a copy may add columns and tables of
its own, and may never rename or drop what the CLI expects.

It drifted three times. Every time, the change was made **here**:

| added by | column | noticed |
|---|---|---|
| `sea-mii7.1/.2` | `positions.claim_key`, `evidence.stance` | same day, by hand |
| the drops work | `link_placement`, `video_views`, `watch_seconds`, `avg_watch_seconds` | five days later, by an AST diff |
| `31afc69` | `positions.superseded_by_id` | 2026-09-03, by this check |

None of them raised. The columns existed in Postgres; follow-whee's models
simply could not see them, so the app read the same database and was blind to
data the CLI was writing — 72 video views and 1,410 watch-seconds on one drop,
`claim_key` on four positions, `stance` on fourteen evidence rows.

The interesting part is not the drift, it is where the check was. After the
second one, follow-whee built the AST diff and put it in follow-whee's suite.
The third drift happened anyway, because **the writer that causes the drift is
not the program that runs that suite.** A session adding a column here has no
reason to open follow-whee at all, and the check that would have stopped it was
sitting in a repo it never touched — uncommitted, as it turned out, so it had
never run anywhere but the one working tree that wrote it.

So the check moved to the writer's side (`strategy_store/schema_contract.py`),
and the borrowed set moved with it, from prose into data:

```python
Consumer(
    name="follow-whee",
    borrows=("drops", "evidence", "forcing", "metric_readings", "metrics", "positions"),
    owns=("beats", "drop_stats"),
    ...
)
```

Declaring `borrows` rather than inferring it buys one thing the consumer-side
diff could not have: a table **renamed** here used to leave `set(store) &
set(copy)` quietly, so the check went green on the change that breaks the
consumer worst. A declared name that is gone upstream is now a violation.

And because the store can add a column but cannot make the commit that applies
it in someone else's repo, an open drift is *recorded* (`KNOWN_DRIFT`) rather
than left to stand the build red — with the symmetrical rule that a record which
no longer reproduces is also a failure. A list of known problems that nobody has
to delete from is prose again, which is what this whole case is about.

## The distinction that matters

| | **Clobber** (cases 1 and 2) | **Drift** (cases 3, 3b and 4) |
|---|---|---|
| Cause | two writers, one column | two stores, one fact, nobody reading |
| Symptom | work silently reverts | both sides internally consistent, jointly false |
| Fix | ownership rules and refusals | a reconciler that reads and reports |
| Detect by | asking *what else writes this column?* | asking *what would prove this still true?* |

A clobber is fixed by **saying no** at write time. A drift cannot be — no
refusal helps, because no illegal write ever happens. It needs something that
periodically re-reads the other system and reports the disagreement, and that
report has to appear somewhere already looked at. A drift report you have to
remember to run reproduces the failure it was built to fix.

## What `gate edit` refuses, and why

`gate edit` exists because correcting a mistake is not the same act as
answering a question. Before it, a gate with wrong prose had to be resolved
and re-added — which rewrites a live deadline and buries the original under a
resolution it never had.

Its two refusals both come from this explainer:

1. **A seed-owned gate cannot be edited here.** Case 2, prevented at the
   source: the file owns that text, so the edit would vanish on the next
   reseed. `seeded_refs()` and `upsert_forcing` share one `key_ref()` so the
   guard and the writer can never disagree about which row a file entry owns.
2. **Moving or clearing a date requires `--note`.** Not an ownership rule —
   a different one. Dated gates are the entire point of the table; a date
   that slides with no reason on the record *is* the deferral-without-a-
   trigger failure this store was built to catch.

It also refuses resolved gates (`gate reopen` first — a resolve note is the
record of a closed decision, not a draft) and no-ops.

## Before you add a field

Four questions, in order:

1. **Who writes it?** Name one system. If the honest answer is two, stop.
2. **What else touches the row?** Grep every writer of that table. A column
   is shared far more often than a table is — and the grep has to leave this
   repo, because one of the writers is another program with its own copy of the
   declaration (case 4). `scripts/check_schema_contract.py` is that grep.
3. **Does a sync overwrite it?** If yes, the store must refuse edits to it —
   with a message naming the real owner.
4. **Does anything outside verify it?** If yes, that is a drift risk, and a
   refusal will not help. It needs a reconciler.

Question 2 is the one that was skipped, and cases 2 and 4 are what it cost —
case 2 inside this repo, case 4 one repo over.
