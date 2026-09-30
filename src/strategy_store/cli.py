"""`strategy` — CLI over the strategy store.

\b
strategy init                       create tables (SQLite by default)
strategy seed [--vikunja] [--beads] insert/refresh positions from the sources
strategy list [--status S] [--element E] [--source SYS] [--all]
strategy show <id> [--full]         one position in full; --full shows complete statement/evidence
strategy set <id> [--status] [--scope] [--element] [--venture] [--statement]
strategy revisions <id>              earlier versions of a rewritten statement
strategy clarity [--element E]       positions a stranger could not read
strategy gate add|edit|resolve|reopen forcing gates
strategy pending                    positions drafted but never adopted
strategy adopt <id>... [--status S] take a drafted position as your own (alias: bless)
strategy return <id>... [--note]    send an adopted position back to the pile
strategy supersede <new_id> <old_id> [--evidence TEXT]
strategy evidence add <id> --source SRC --summary TEXT [--scope] [--on DATE]
strategy evidence list <id> [--on DATE]  read all evidence for a position
strategy reconcile                  report source-vs-store drift, change nothing
strategy claim <id>... [--key K]    gather rows answering ONE question into one claim
strategy assess [venture] [test]    answer a framework test for a venture (blank = unanswered)
strategy doctor                     which store, which settings, how stale
strategy profile set|claim|check    public surfaces, checked against the store
strategy triage                     walk the pending pile and decide each one
strategy reject <id>... --note      decide NOT to hold a position (dated, retired)
strategy factor <id> --kind --text  a reason for/against, or the test that settles it
strategy ask <id> "..."             what must be known before this can be decided
strategy questions                  open questions — the worklist triage produces
strategy web [--port 8021]          the admin surface: the read side, browsable
"""

from __future__ import annotations

import pathlib
from datetime import date, datetime

import click
from sqlalchemy import func, select

from . import beadlink, core, db, env, queries
from . import seed as seed_mod
from .frameworks import FRAMEWORKS
from .models import (
    CASCADE_DIRECTIONS,
    CASCADE_STANDINGS,
    FACTOR_KINDS,
    LICENSE_KINDS,
    MEMBERSHIP_MODES,
    POSITION_PREDICATES,
    SCOPES,
    STANCES,
    STATUSES,
    SURFACES,
    VERDICTS,
    Assessment,
    Evidence,
    Position,
    ProfileClaim,
    ProfileField,
)

STATUS_MARK = {"holding": "○", "contested": "◐", "resolved": "✓", "retired": "❄"}


@click.group(help=__doc__)
def main():
    pass


def _check_lengths(model, **values) -> None:
    """Refuse over-long values before the INSERT, with the column that is too long.

    Postgres raises StringDataRightTruncation on the flush, which reaches the user
    as a hundred-line SQLAlchemy traceback naming no column and no value — the
    failure that put a 62-character source_ref past `add` on 2026-08-28. Limits
    are read off the model so they cannot drift from the schema."""
    # Column name -> the flag the user actually typed, where they differ.
    flags = {"source_ref": "source", "element_key": "element"}
    for name, value in values.items():
        if value is None:
            continue
        limit = getattr(model.__table__.columns[name].type, "length", None)
        if limit and len(str(value)) > limit:
            flag = flags.get(name, name.replace("_", "-"))
            raise click.ClickException(
                f"--{flag} is {len(str(value))} characters; "
                f"{model.__tablename__}.{name} holds {limit}.\n"
                f"  {str(value)[:limit]}<-- cut here\n"
                f"  (the long form belongs in --statement, which is unbounded text)"
            )


@main.command()
def init():
    """Create the tables."""
    db.init_db()
    click.echo(f"initialised {env.mask(db.db_url())}")


@main.command(name="seed")
@click.option("--vikunja/--no-vikunja", default=True)
@click.option("--beads/--no-beads", default=True)
def seed_cmd(vikunja: bool, beads: bool):
    """Insert new positions from the sources; refresh text on existing ones."""
    db.init_db()
    rows: list[dict] = []
    if vikunja:
        v = seed_mod.fetch_vikunja()
        click.echo(f"vikunja: {len(v)} open tasks in projects {sorted(seed_mod.VIKUNJA_PROJECTS)}")
        rows += v
    if beads:
        b = seed_mod.fetch_beads()
        click.echo(f"beads:   {len(b)} decision issues across rigs")
        rows += b
    with db.session() as s:
        c = seed_mod.upsert(s, rows)
    click.echo(f"inserted {c['inserted']} · refreshed {c['updated']}")
    for key, old, new, st_store, st_src in c["drift"]:
        click.echo(
            f"  DRIFT {key}: source {old!r} → {new!r} (store keeps status={st_store}; "
            f"source now maps to {st_src})"
        )
    for new_key, old_key, why in c.get("refused", []):
        click.echo(f"  REFUSED supersede {new_key} → {old_key}: {why}")


def _fmt(p: Position, superseded_ids: set[int]) -> str:
    sup = " (superseded)" if p.id in superseded_ids else ""
    el = p.element_key or "—"
    return (
        f"{STATUS_MARK[p.status]} {p.id:>3}  {p.status:<9} {p.scope:<10} {el:<9} "
        f"{p.source_system}:{p.source_ref:<14} {p.title[:70]}{sup}"
    )


@main.command(name="list")
@click.option("--status", type=click.Choice(STATUSES))
@click.option("--element", help="e.g. ptw.box3, or 'none' for orphans")
@click.option("--source", type=click.Choice(["vikunja", "beads", "doc", "manual"]))
@click.option("--all", "show_all", is_flag=True,
              help="include superseded rows and candidate-cascade hypotheses")
def list_cmd(status, element, source, show_all):
    """Current positions (superseded and candidate-only rows hidden unless --all).

    Reads through `queries.positions` rather than its own SELECT. It had its own
    for a while, which is how the candidate-cascade gate landed in the shared
    reader and in prime while `strategy list` went on printing hypotheses
    (solutions-tx3.1). One reader, so the surfaces cannot disagree.
    """
    with db.session() as s:
        rows, superseded = queries.positions(
            s, status=status, element=element, source=source, show_all=show_all
        )
        for p in rows:
            click.echo(_fmt(p, superseded))
        click.echo(f"-- {len(rows)} positions")


@main.command()
@click.argument("pid", type=int)
@click.option("--full", is_flag=True, help="print complete statement and evidence summaries")
def show(pid: int, full: bool):
    """One position in full: statement, supersession chain, and its evidence.

    By default, statement is limited to 40 lines and evidence to 100 chars.
    Use --full to print everything in complete form."""
    with db.session() as s:
        p = s.get(Position, pid)
        if not p:
            raise click.ClickException(f"no position {pid}")
        click.echo(f"#{p.id} {p.title}")
        click.echo(
            f"  status={p.status} scope={p.scope} element={p.element_key} venture={p.venture}"
        )
        click.echo(
            f"  source={p.source_system}:{p.source_ref} (source_status={p.source_status}) "
            f"decided_on={p.decided_on} synced={p.synced_at:%Y-%m-%d %H:%M}"
            if p.synced_at
            else f"  source={p.source_system}:{p.source_ref}"
        )
        for q in p.supersedes:
            click.echo(f"  supersedes #{q.id} {q.title[:60]}")
        if p.superseded_by:
            click.echo(f"  SUPERSEDED BY #{p.superseded_by.id} {p.superseded_by.title[:56]}")
        if p.statement:
            click.echo("")
            lines = p.statement.splitlines() if full else p.statement.splitlines()[:40]
            click.echo("\n".join("  " + ln for ln in lines))
        if p.evidence:
            click.echo("\n  evidence:")
            for e in p.evidence:
                summary = e.summary if full else e.summary[:100]
                click.echo(f"    [{e.scope}] {e.observed_on or ''} {e.source}: {summary}")


@main.command(name="set")
@click.argument("pid", type=int)
@click.option("--status", type=click.Choice(STATUSES))
@click.option("--scope", type=click.Choice(SCOPES))
@click.option("--element", help="framework slot, or 'none' to clear")
@click.option("--venture")
@click.option("--title", help="manual/doc positions only — synced text belongs upstream")
@click.option("--statement", help="the CLAIM, ideally one sentence; the case for it "
                                  "goes in `factor`, observations in `evidence`. "
                                  "manual/doc positions only")
@click.option("--statement-file", type=click.Path(exists=True, dir_okay=False),
              help="read --statement from a file, for text with newlines or quotes")
@click.option("--note", help="why the text changed — stored with the superseded version")
def set_cmd(pid, status, scope, element, venture, title, statement, statement_file, note):
    """Edit store-owned columns.

    Text (--title/--statement) is editable only on manual and doc positions:
    anything synced from vikunja or beads gets its text back from the source."""
    if statement_file:
        if statement:
            raise click.ClickException("pass --statement or --statement-file, not both")
        statement = pathlib.Path(statement_file).read_text().strip()
    with db.session() as s:
        try:
            p = core.set_fields(s, pid, status=status, scope=scope,
                                element_key=element, venture=venture,
                                title=title, statement=statement, note=note)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(_fmt(p, set()))


@main.command()
@click.argument("pid", type=int)
@click.option("--full", is_flag=True, help="print each superseded statement in full")
def revisions(pid, full):
    """Earlier versions of a position's text, newest first.

    Every write that changes a title or statement snapshots what it replaced, so
    a compression pass is reversible and a bad summary is recoverable."""
    with db.session() as s:
        p = s.get(Position, pid)
        if p is None:
            raise click.ClickException(f"no position {pid}")
        rows = sorted(p.revisions, key=lambda r: r.replaced_at, reverse=True)
        if not rows:
            click.echo(f"#{pid} has no earlier versions — its text has never been rewritten.")
            return
        click.echo(f"#{pid} {p.title}")
        click.echo(f"-- {len(rows)} earlier version(s)")
        for r in rows:
            when = r.replaced_at.strftime("%Y-%m-%d %H:%M")
            click.echo(f"\n  {when}  changed: {r.changed}" + (f"  ({r.note})" if r.note else ""))
            if r.title and r.title != p.title:
                click.echo(f"    title was: {r.title}")
            body = r.statement or ""
            if not full:
                words = body.split()
                if len(words) > 45:
                    body = " ".join(words[:45]) + f" ... [{len(words)} words, --full for all]"
            click.echo("\n".join("    " + ln for ln in body.splitlines()) if body else "    (empty)")


@main.command()
@click.option("--element", help="one framework slot, e.g. ptw.box2")
@click.option("--editable-only", is_flag=True, help="only manual/doc positions, which `set` can fix")
@click.option("--include-retired", is_flag=True)
def clarity(element, editable_only, include_retired):
    """Positions a stranger could not read, and why.

    The bar: a position is a CLAIM, in one or two sentences, that survives being
    read by someone without the store open. The case for it belongs in `factor`,
    observations in `evidence`. Every rule here was learned from a statement this
    store had to retire."""
    with db.session() as s:
        rows = core.clarity(s, element=element, include_retired=include_retired)
    if editable_only:
        rows = [r for r in rows if r["editable"]]
    if not rows:
        click.echo("every position reads clean.")
        return
    cur = None
    for r in rows:
        if r["element"] != cur:
            cur = r["element"]
            click.echo(f"\n── {cur or '(no element)'} ──")
        ed = "" if r["editable"] else "  [synced — fix upstream]"
        click.echo(f"  #{r['id']:<3} {r['title'][:58]}{ed}")
        for kind, why in r["fails"]:
            click.echo(f"        {kind}: {why}")
    n = len(rows)
    fixable = sum(1 for r in rows if r["editable"])
    click.echo(f"\n-- {n} position(s) below the bar, {fixable} editable here")


@main.group()
def profile():
    """Public surfaces (LinkedIn and friends) as a query over the store.

    The store holds the positions, the evidence and the drops. The profile is the
    one public surface that is not derived from them, so it drifts silently —
    a cadence claim the drop count does not support, memo shorthand pasted into
    public copy. `profile check` is that comparison (sea-mii7.2).
    """


@profile.command("set")
@click.argument("field")
@click.option("--text", required=True)
@click.option("--surface", type=click.Choice(SURFACES), default="linkedin")
@click.option("--limit", "char_limit", type=int, default=None,
              help="the surface's own character limit for this field")
@click.option("--ordinal", type=int, default=None, help="display order on the surface")
def profile_set(field, text, surface, char_limit, ordinal):
    """Record what the surface CURRENTLY says in this field."""
    from . import profile as prof

    with db.session() as s:
        f = s.scalar(
            select(ProfileField).where(
                ProfileField.surface == surface, ProfileField.field == field
            )
        )
        if f is None:
            f = ProfileField(surface=surface, field=field)
            s.add(f)
        f.text = text
        f.char_limit = char_limit or prof.FIELD_LIMITS.get(field)
        if ordinal is not None:
            f.ordinal = ordinal
        prof.touch(f)
        s.commit()
        limit = f.char_limit
        over = f"  ⚠ OVER by {len(text) - limit}" if limit and len(text) > limit else ""
        click.echo(f"{surface}/{field}: {len(text)} chars" + (f"/{limit}" if limit else "") + over)


@profile.command("claim")
@click.argument("field")
@click.option("--text", "claim_text", required=True, help="the factual claim, in its own words")
@click.option("--license", "licenses", multiple=True,
              help="what licenses it: position:20, drop:13, metric:li-newsletter-subs, app:purr")
@click.option("--surface", type=click.Choice(SURFACES), default="linkedin")
@click.option("--note", default="")
def profile_claim(field, claim_text, licenses, surface, note):
    """Attach a claim to a field, and say what licenses it.

    No --license is a legal call and is the point: an unlicensed claim is
    recorded as unlicensed so `profile check` can name it, rather than refused
    and forgotten.
    """
    from . import profile as prof

    with db.session() as s:
        f = s.scalar(
            select(ProfileField).where(
                ProfileField.surface == surface, ProfileField.field == field
            )
        )
        if f is None:
            raise click.ClickException(f"no {surface}/{field} — `strategy profile set` it first")
        specs = [prof.split_license(x) for x in licenses] or [("none", None)]
        for kind, ident in specs:
            if kind not in LICENSE_KINDS:
                raise click.ClickException(
                    f"licence kind must be one of {', '.join(LICENSE_KINDS)}"
                )
            s.add(
                ProfileClaim(
                    field_id=f.id, claim_text=claim_text,
                    licensed_by_kind=kind, licensed_by_id=ident, note=note,
                )
            )
        prof.touch(f)
        s.commit()
    shown = ", ".join(f"{k}:{i}" if i else k for k, i in specs)
    click.echo(f"{surface}/{field}: “{claim_text[:50]}” ← {shown}")
    if not licenses:
        click.echo("  UNLICENSED — `profile check` will report it.")


@profile.command("list")
@click.option("--surface", type=click.Choice(SURFACES), default="linkedin")
def profile_list(surface):
    """Fields, their length, and their claims."""
    from . import profile as prof

    with db.session() as s:
        fields = prof._fields(s, surface)
        if not fields:
            click.echo(f"nothing recorded for {surface}.")
            return
        for f in fields:
            limit = f.char_limit
            click.echo(
                f"{f.field:<20} {len(f.text):>5}"
                + (f"/{limit}" if limit else "")
                + f"  updated {f.updated_at:%Y-%m-%d}"
            )
            for c in f.claims:
                lic = (
                    f"{c.licensed_by_kind}:{c.licensed_by_id}"
                    if c.licensed_by_id
                    else "UNLICENSED"
                )
                click.echo(f"    - {c.claim_text[:64]:<64} {lic}")


@profile.command("export")
@click.option("--surface", type=click.Choice(SURFACES), default="linkedin")
def profile_export(surface):
    """Render the fields in surface order with character counts, ready to paste."""
    from . import profile as prof

    with db.session() as s:
        click.echo(prof.export(s, surface))


@profile.command("check")
@click.option("--surface", type=click.Choice(SURFACES), default="linkedin")
def profile_check(surface):
    """Compare the public wording against what the store can license.

    Exits non-zero when something is found, so it can gate a paste.
    """
    from . import profile as prof

    with db.session() as s:
        findings = prof.check(s, surface)
    hits = 0
    for f in findings:
        if not f["ran"]:
            click.echo(f"– {f['check']}: could not run")
            click.echo(f"    {f['why']}")
            continue
        if not f["rows"]:
            click.echo(f"✓ {f['check']}")
            continue
        hits += len(f["rows"])
        click.echo(f"✗ {f['check']} ({len(f['rows'])})")
        click.echo(f"    {f['why']}")
        for r in f["rows"]:
            click.echo(f"    - {r}")
    click.echo(f"-- {hits} to fix on {surface}")
    if hits:
        raise SystemExit(1)


@main.command(name="claim")
@click.argument("pids", type=int, nargs=-1, required=True)
@click.option("--key", "claim_key", help="shared key; default is derived from the first id")
@click.option("--clear", is_flag=True, help="ungroup these positions instead")
def claim_cmd(pids, claim_key, clear):
    """Gather positions that answer the SAME question into one claim.

    Three systems answering one question is one dispute, not three arguments,
    and the report renders a gathered claim as a single contested position with
    each source attached as evidence. Use `supersede` when one row REPLACED
    another; use this when they are all still live and disagree (sea-mii7.1).
    """
    with db.session() as s:
        try:
            key, rows, contested = core.claim(s, pids, key=claim_key, clear=clear)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        if clear:
            click.echo(f"ungrouped {len(rows)} position(s)")
            return
        click.echo(
            f"claim '{key}': "
            + " · ".join(f"#{r.id} {r.source_system}:{r.source_ref}" for r in rows)
        )
        if contested:
            click.echo("  renders as ONE contested claim; the other sources become its evidence.")


@main.command(name="assess")
@click.argument("venture", required=False)
@click.argument("test_key", required=False)
@click.option("--verdict", type=click.Choice(VERDICTS))
@click.option("--note", default="", help="why — the part a mark cannot carry")
@click.option("--scope", type=click.Choice(SCOPES), default="single-rig",
              help="what the answerer could see")
@click.option("--framework", default="ptw")
@click.option("--on", "decided_on", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--clear", is_flag=True, help="remove this answer, returning the cell to blank")
def assess_cmd(venture, test_key, verdict, note, scope, framework, decided_on, clear):
    """Answer one framework test for one venture — or list what is answered.

    With no arguments this prints the grid. A cell nobody has answered stays
    BLANK in the report: seven tests over a dozen ventures is 84 answers, and
    defaulting them to 'pass' would manufacture opinions nobody holds.
    """
    fw = FRAMEWORKS.get(framework)
    if not fw:
        raise click.ClickException(f"unknown framework '{framework}'")
    tests = dict(fw["tests"])

    if not venture:
        with db.session() as s:
            rows = sorted(
                s.scalars(select(Assessment).where(Assessment.framework == framework)),
                key=lambda a: (a.venture, a.test_key),
            )
        if not rows:
            click.echo("nothing answered yet. Questions:")
            for k, text in fw["tests"]:
                click.echo(f"  {k}  {text}")
            return
        for a in rows:
            click.echo(f"{a.venture:<26} {a.test_key:<4} {a.verdict:<11} {a.note[:60]}")
        click.echo(f"-- {len(rows)} answered")
        return

    if not test_key or test_key not in tests:
        raise click.ClickException(
            f"test must be one of {', '.join(tests)} — run `strategy assess` to see them"
        )
    with db.session() as s:
        existing = s.scalar(
            select(Assessment).where(
                Assessment.framework == framework,
                Assessment.test_key == test_key,
                Assessment.venture == venture,
            )
        )
        if clear:
            if not existing:
                raise click.ClickException("that cell is already blank")
            s.delete(existing)
            s.commit()
            click.echo(f"{venture} {test_key}: cleared")
            return
        if not verdict:
            raise click.ClickException("--verdict is required (pass | fail | irrelevant)")
        if existing:
            existing.verdict, existing.note, existing.scope = verdict, note, scope
            existing.decided_on = decided_on.date() if decided_on else existing.decided_on
        else:
            s.add(
                Assessment(
                    framework=framework, test_key=test_key, venture=venture, verdict=verdict,
                    note=note, scope=scope,
                    decided_on=decided_on.date() if decided_on else date.today(),
                )
            )
        s.commit()
    click.echo(f"{venture} {test_key} = {verdict}  ({tests[test_key]})")


@main.command()
@click.option("--element", default=None, help="only this box, e.g. ptw.box4")
def pending(element):
    """Positions drafted but never adopted — the pile awaiting a decision.

    'Drafted' is `decided_on IS NULL`: a claim nobody has dated is a claim nobody
    has decided, the same way an undated gate is itself a finding. Until `adopt`
    existed the state lived in the statement PROSE ('PROPOSED, needs blessing'),
    which no report could see.
    """
    with db.session() as s:
        rows = queries.pending(s, element)
        if not rows:
            click.echo("nothing open — no undated drafts, no live disputes, no open questions.")
            return
        for p in rows:
            el = p.element_key or "—"
            click.echo(f"{STATUS_MARK[p.status]} {p.id:>3}  {p.status:<9} {el:<9} {p.title[:78]}")
        click.echo(f"-- {len(rows)} awaiting adoption")


@main.command()
@click.argument("pids", type=int, nargs=-1, required=True)
@click.option("--on", "decided_on", type=click.DateTime(["%Y-%m-%d"]),
              help="the date the choice was actually made (default: today)")
@click.option("--status", type=click.Choice(STATUSES),
              help="also move the status, e.g. --status resolved")
@click.option("--note", help="why you adopted it — recorded as evidence")
@click.option("--redate", is_flag=True,
              help="allow re-dating a position that already carries a decided date")
def adopt(pids, decided_on, status, note, redate):
    """Take a drafted position as your own — the choice is now yours, not a draft.

    ADOPT, not 'approve': the point is not that an agent's work passed review, it
    is that the claim has become YOURS. The store does not record who typed it,
    and should not — a position is a stance someone holds, and adoption is the
    moment someone starts holding it. (`bless` is kept as an alias for muscle
    memory; it framed this as sign-off on another's work, which is the wrong
    relationship.)

    Adoption stamps `decided_on` and writes an Evidence row, so the act has a date
    and a trail rather than silently flipping a field.
    """
    when = decided_on.date() if decided_on else date.today()
    with db.session() as s:
        try:
            for p, was in core.adopt(s, pids, when=when, status=status, note=note,
                                     redate=redate):
                moved = f" ({was} -> {p.status})" if status and status != was else ""
                click.echo(f"#{p.id} adopted {when}{moved}  {p.title[:60]}")
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()


main.add_command(adopt, name="bless")


@main.command(name="return")
@click.argument("pids", type=int, nargs=-1, required=True)
@click.option("--note", help="what needs to change before it can be adopted")
def return_cmd(pids, note):
    """Send a position back to the pile — it was dated, but it is not decided.

    The counterpart to `adopt`, and the first real use of adoption produced the
    need for it: a position can carry a decided date it never earned, because a
    session stamped one on a draft. Clearing the date returns it to `pending`,
    which is exactly what "back to committee" means — undecided and visible again.

    This does NOT retire or contest the position. `retired` means no longer held;
    `contested` means disputed. Neither describes a claim that simply is not ready,
    and squeezing this into one of them would lose the distinction the pile exists
    to keep.
    """
    with db.session() as s:
        try:
            for p, was in core.send_back(s, pids, note=note):
                click.echo(f"#{p.id} returned (was dated {was})  {p.title[:60]}")
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()


def _wrap(text: str, indent: str = "      ", width: int = 92) -> str:
    """Wrap for the triage card. A bullet marker belongs on the FIRST line only —
    repeating it down a wrapped paragraph reads as four separate factors."""
    import textwrap

    rest = " " * len(indent)
    out = []
    for para in (text or "").splitlines():
        out.extend(
            textwrap.wrap(para, width=width, initial_indent=indent, subsequent_indent=rest)
            or [indent.rstrip()]
        )
        indent = rest  # only the first physical line of the first paragraph is marked
    return "\n".join(out)


def _card(d: dict, full: bool = False) -> str:
    """One position, with everything needed to decide it. Same dossier the web
    card will render, so the two cannot show a different case."""
    p = d["position"]
    L = []
    L.append(f"#{p.id}  {p.title}")
    L.append(f"      {p.element_key or 'no box'} · {p.scope} · {p.status} · "
             f"{p.source_system}:{p.source_ref}")
    for g in d["gates"]:
        when = g.due_on.isoformat() if g.due_on else "NO DATE"
        L.append(f"      GATE {when}  {g.title}")
    body = p.statement or ""
    if not full:
        lines = [ln for ln in body.splitlines() if ln.strip()][:4]
        body = "\n".join(lines)
    if body.strip():
        L.append("")
        L.append(_wrap(body))
        if not full and len((p.statement or "").splitlines()) > 5:
            L.append("      … [v] for the full statement")
    for kind, label in (("for", "FOR"), ("against", "AGAINST"), ("test", "WOULD SETTLE IT")):
        rows = d["factors"][kind]
        if rows:
            L.append("")
            L.append(f"  {label}")
            for f in rows:
                L.append(_wrap(f.text, indent="      · "))
    for stance, label in (("supports", "EVIDENCE FOR"), ("opposes", "EVIDENCE AGAINST"),
                          ("complicates", "EVIDENCE, COMPLICATES")):
        rows = d["evidence"][stance]
        if rows:
            L.append("")
            L.append(f"  {label}")
            for e in rows:
                when = e.observed_on.isoformat() if e.observed_on else "undated"
                L.append(_wrap(f"{when} {e.source}: {e.summary}", indent="      · "))
    if d["evidence"]["unstated"]:
        L.append("")
        L.append(f"  {len(d['evidence']['unstated'])} further observation(s), stance not recorded")
    if d["open_questions"]:
        L.append("")
        L.append("  OPEN QUESTIONS")
        for q in d["open_questions"]:
            L.append(_wrap(f"q{q.id} ({q.asked_on}): {q.text}", indent="      ? "))
    if d["bare"]:
        L.append("")
        L.append("  ⚠ no case recorded either way — this is why it has not been decided")
    return "\n".join(L)


@main.command()
@click.option("--element", default=None, help="only this box, e.g. ptw.box4")
@click.option("--id", "only_id", type=int, default=None, help="just this position")
@click.option("--pending-only", is_flag=True,
              help="only never-dated drafts; omit to include live disputes and blocked rows")
def triage(element, only_id, pending_only):
    """Walk every OPEN issue one at a time and decide each one.

    Shows the whole case — the claim, the arguments either side, the observations
    either side, what would settle it, and what is still unknown — then takes the
    decision. Three outcomes and an escape hatch: adopt (I hold this), reject (I
    do not), question (I would decide if I knew X), skip.

    Every action goes through `core`, the same code a web surface calls, so the
    invariants cannot differ between them (position #67).
    """
    with db.session() as s:
        rows = [queries.dossier(s, only_id).get("position")] if only_id \
            else queries.triage_queue(s, element, pending_only=pending_only)
        rows = [r for r in rows if r is not None]
        if not rows:
            click.echo("nothing pending — every position carries a decided date.")
            return
        click.echo(f"{len(rows)} to triage. "
                   f"[a]dopt [r]eject [q]uestion [n]ote [f]actor [v]iew [s]kip [x]quit")
        decided = {"adopted": 0, "rejected": 0, "asked": 0, "skipped": 0}
        for i, p in enumerate(rows, 1):
            full = False
            while True:
                d = queries.dossier(s, p.id)
                click.echo("\n" + "─" * 96)
                click.echo(f"[{i}/{len(rows)}]  " + _card(d, full=full))
                click.echo("─" * 96)
                ch = click.prompt("  ", default="s", show_default=False).strip().lower()[:1]
                try:
                    if ch == "a":
                        note = click.prompt("  why you hold it (enter to skip)", default="",
                                            show_default=False) or None
                        core.adopt(s, [p.id], note=note)
                        s.commit()
                        decided["adopted"] += 1
                        click.echo(f"  ✓ #{p.id} adopted")
                        break
                    if ch == "r":
                        note = click.prompt("  why you do not hold it (required)")
                        core.reject(s, [p.id], note=note)
                        s.commit()
                        decided["rejected"] += 1
                        click.echo(f"  ✗ #{p.id} rejected")
                        break
                    if ch == "q":
                        core.ask(s, p.id, text=click.prompt("  what would you need to know"))
                        s.commit()
                        decided["asked"] += 1
                        click.echo(f"  ? #{p.id} flagged — stays pending, now blocked")
                        break
                    if ch == "n":
                        stance = click.prompt("  stance", default="",
                                              type=click.Choice(["", *STANCES]),
                                              show_default=False) or None
                        core.add_evidence(s, p.id, source="triage note",
                                          summary=click.prompt("  note"),
                                          scope=p.scope, observed_on=date.today(),
                                          stance=stance)
                        s.commit()
                        click.echo("  noted")
                        continue
                    if ch == "f":
                        kind = click.prompt("  kind", type=click.Choice(FACTOR_KINDS))
                        core.add_factor(s, p.id, kind=kind, text=click.prompt("  factor"),
                                        source="triage")
                        s.commit()
                        continue
                    if ch == "v":
                        full = not full
                        continue
                    if ch == "x":
                        click.echo("  stopped.")
                        _triage_summary(decided, len(rows), i - 1)
                        return
                    decided["skipped"] += 1
                    break
                except core.InvariantError as exc:
                    click.echo(f"  refused: {exc}")
        _triage_summary(decided, len(rows), len(rows))


def _triage_summary(d: dict, total: int, seen: int) -> None:
    click.echo(
        f"\n-- {seen}/{total} seen · {d['adopted']} adopted · {d['rejected']} rejected · "
        f"{d['asked']} flagged with a question · {d['skipped']} skipped"
    )
    if d["asked"]:
        click.echo("   the questions are the worklist: `strategy questions`")


@main.command()
@click.argument("pids", type=int, nargs=-1, required=True)
@click.option("--note", required=True, help="why you do not hold it — required")
@click.option("--on", "decided_on", type=click.DateTime(["%Y-%m-%d"]), default=None)
def reject(pids, note, decided_on):
    """Decide NOT to hold a position: retired, and dated.

    The third triage outcome. `adopt` says I hold this; `return` says not ready;
    this says no. Dated on purpose — deciding you do not hold something is a
    decision, and an undated rejection sits in the pending pile forever looking
    like work nobody got to.
    """
    with db.session() as s:
        try:
            for p, was in core.reject(s, pids, when=decided_on.date() if decided_on else None,
                                      note=note):
                click.echo(f"#{p.id} rejected ({was} -> retired)  {p.title[:60]}")
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()


@main.command()
@click.argument("pid", type=int)
@click.option("--kind", type=click.Choice(FACTOR_KINDS), required=True,
              help="for / against / test (the thing that would settle it)")
@click.option("--text", required=True)
@click.option("--source", default="", help="who reasoned it")
def factor(pid, kind, text, source):
    """Record a reason to adopt, a reason not to, or the test that would settle it.

    A factor is an ARGUMENT; evidence is an OBSERVATION. Positions stalled because
    they carried the claim and neither of these.
    """
    with db.session() as s:
        try:
            core.add_factor(s, pid, kind=kind, text=text, source=source)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"#{pid} {kind}: {text[:70]}")


@main.command()
@click.argument("pid", type=int)
@click.argument("text")
def ask(pid, text):
    """Record what must be known before this position can be decided."""
    with db.session() as s:
        try:
            q = core.ask(s, pid, text=text)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"question {q.id} on #{pid}: {text[:70]}")


@main.command()
@click.argument("qid", type=int)
@click.argument("text")
def answer(qid, text):
    """Answer an open question, unblocking its position."""
    with db.session() as s:
        try:
            q = core.answer(s, qid, text=text)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        pid = q.position_id
        s.commit()
        click.echo(f"question {qid} answered — #{pid} is unblocked")


@main.command()
def questions():
    """Open questions, by position — the worklist triage produces."""
    with db.session() as s:
        rows = core.open_questions(s)
        for q in rows:
            p = s.get(Position, q.position_id)
            click.echo(f"q{q.id:>3}  #{q.position_id} {(p.title if p else '')[:52]}")
            click.echo(f"      asked {q.asked_on}: {q.text}")
        click.echo(f"-- {len(rows)} open")


@main.command()
@click.argument("new_id", type=int)
@click.argument("old_id", type=int)
@click.option(
    "--evidence", "evidence_note", help="what new evidence justifies a narrower-scope supersession"
)
def supersede(new_id, old_id, evidence_note):
    """Mark NEW as superseding OLD, subject to the scope rule."""
    with db.session() as s:
        try:
            new, why = core.supersede(s, new_id, old_id, evidence_note=evidence_note)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"#{new.id} now supersedes #{old_id} ({why})")


@main.group()
def evidence():
    """Add or read observations on a position."""
    pass


@evidence.command(name="add")
@click.argument("pid", type=int)
@click.option("--source", required=True)
@click.option("--summary", required=True)
@click.option("--scope", type=click.Choice(SCOPES), default="single-rig")
@click.option("--on", "observed_on", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--stance", type=click.Choice(STANCES), default=None,
              help="which way it cuts; omit when you are only recording that it happened")
def evidence_add(pid, source, summary, scope, observed_on, stance):
    """Attach an observation to a position."""
    with db.session() as s:
        try:
            core.add_evidence(s, pid, source=source, summary=summary, scope=scope,
                              observed_on=observed_on.date() if observed_on else None,
                              stance=stance)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo("ok")


@evidence.command(name="list")
@click.argument("pid", type=int)
@click.option("--on", "date_filter", type=click.DateTime(["%Y-%m-%d"]),
              help="filter to observations from this date")
def evidence_list(pid, date_filter):
    """Read all evidence rows for a position, in full, newest last."""
    with db.session() as s:
        p = s.get(Position, pid)
        if not p:
            raise click.ClickException(f"no position {pid}")
        rows = sorted(p.evidence, key=lambda e: e.created_at)
        if date_filter:
            filter_date = date_filter.date()
            rows = [e for e in rows if e.observed_on == filter_date]
        for e in rows:
            click.echo(f"[{e.scope}] {e.observed_on or 'undated'} · {e.source}")
            click.echo(f"  {e.summary}")
            if e.stance:
                click.echo(f"  stance: {e.stance}")
            click.echo()


@main.command()
@click.option("--no-gates", is_flag=True,
              help="skip the rig reads; the free checks still run")
def reconcile(no_gates: bool):
    """Report where the store and the world disagree. Changes nothing.

    Two halves, and they are different failures. POSITIONS drift because a source
    system's status moved under a row the store owns. GATES drift because nobody
    ever read the work on the gate's behalf — no illegal write happens, both sides
    stay internally consistent, and they are jointly false."""
    with db.session() as s:
        rows = queries.drift(s)
        click.echo("POSITIONS")
        for d in rows:
            p = d["position"]
            click.echo(
                f"  #{p.id} {p.source_system}:{p.source_ref} store={d['store']} "
                f"source-maps-to={d['source_maps_to']}  {p.title[:60]}"
            )
        click.echo(
            f"-- {len(rows)} drifted (store is authoritative for status; "
            f"edit with `strategy set`)"
        )
        # Free — no rig, no subprocess — so it runs even under --no-gates, which
        # exists to skip the reads that cost something.
        from .forcing_seed import reseed_drift

        seedy = reseed_drift(s)
        click.echo("\nSEED FILE vs STORE")
        for d in seedy:
            r = d["row"]
            if d["kind"] == "clear":
                what = (
                    f"store has {d['store_due']}, the file has none — a reseed would "
                    f"UNDATE this gate, and an undated gate drops out of every count "
                    f"of what is coming"
                )
            else:
                what = (
                    f"store has {d['store_due']}, the file says {d['file_due']} — "
                    f"a reseed would move it"
                )
            click.echo(f"  [{d['kind'].upper()}] #{r.id} {r.title[:58]}")
            click.echo(f"      {what}")
            click.echo(f"      fix in the file ({d['source']}), not here — it owns the date")
        click.echo(
            f"-- {len(seedy)} live gates a reseed would re-date"
            + ("" if seedy else " (the file and the store agree on every date)")
        )

        if no_gates:
            return
        findings = beadlink.check(s)
        click.echo("\nGATES")
        for f in findings:
            due = f.due_on.isoformat() if f.due_on else "NO DATE"
            stale = f" ({f.stale_days}d ago)" if f.stale_days is not None else ""
            click.echo(f"  [{_DRIFT_LABEL[f.kind]}] #{f.gate_id} {due} {f.gate_title[:60]}")
            click.echo(f"      {f.detail[:170]}{stale}")
        click.echo(f"-- {_drift_summary(findings)}")


if __name__ == "__main__":
    main()


# ------------------------------------------------------------ export/import
# Backup follows the beads pattern: the live store is a local DB, the durable
# copy is a JSONL export committed to a private git repo. Set STRATEGY_EXPORT_PATH
# to that file; it defaults to ~/.strategy/positions.jsonl.

import json  # noqa: E402
from pathlib import Path  # noqa: E402

DEFAULT_EXPORT = env.path_value(
    "STRATEGY_EXPORT_PATH", Path.home() / ".strategy" / "positions.jsonl"
)
# Raw per-post network exports land here after ingest, alongside the JSONL
# backup. The store keeps only each post's latest numbers; the files keep the
# per-day history.
DEFAULT_DROPS_ARCHIVE = DEFAULT_EXPORT.parent / "drops" / "exports"


def _row(p: Position) -> dict:
    return {
        "source": f"{p.source_system}:{p.source_ref}",
        "title": p.title,
        "statement": p.statement,
        "element_key": p.element_key,
        "status": p.status,
        "scope": p.scope,
        "venture": p.venture,
        "decided_on": p.decided_on.isoformat() if p.decided_on else None,
        "source_status": p.source_status,
        "supersedes": [f"{q.source_system}:{q.source_ref}" for q in p.supersedes] or None,
        "evidence": [
            {
                "source": e.source,
                "scope": e.scope,
                "summary": e.summary,
                "observed_on": e.observed_on.isoformat() if e.observed_on else None,
            }
            for e in p.evidence
        ],
    }


def _drop_row(d) -> dict:
    return {
        "url": d.url,
        "activity_id": d.activity_id,
        "share_id": d.share_id,
        "posted_on": d.posted_on.isoformat(),
        "title": d.title,
        "format": d.format,
        "app_url": d.app_url,
        "impressions": d.impressions,
        "reached": d.reached,
        "article_views": d.article_views,
        "email_sends": d.email_sends,
        "reactions": d.reactions,
        "comments": d.comments,
        "reposts": d.reposts,
        "saves": d.saves,
        "link_clicks": d.link_clicks,
        "followers_gained": d.followers_gained,
        "own_comments": d.own_comments or 0,
        "own_reposts": d.own_reposts or 0,
        "stats_as_of": d.stats_as_of.isoformat() if d.stats_as_of else None,
        "notes": d.notes or "",
    }


def _drops_path(positions_path: Path) -> Path:
    return positions_path.parent / "drops.jsonl"


def _metrics_path(positions_path: Path) -> Path:
    return positions_path.parent / "metrics.jsonl"


def _metric_row(m) -> dict:
    return {
        "name": m.name, "title": m.title, "beat": m.beat, "source": m.source, "source_key": m.source_key,
        "cadence": m.cadence, "gates": m.gates, "due_on": m.due_on.isoformat() if m.due_on else None,
        "position_id": m.position_id, "notes": m.notes,
        "readings": [
            {"as_of": r.as_of.isoformat(), "value": r.value, "origin": r.origin, "note": r.note}
            for r in m.readings
        ],
    }


@main.command()
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help=f"JSONL backup target (default: {DEFAULT_EXPORT})",
)
@click.option(
    "--ctx",
    "ctx_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="write the ctx decisions corpus (one markdown file per position) here instead "
    "of the JSONL backup; regenerated every call, never hand-edited",
)
def export(out: Path | None, ctx_dir: Path | None):
    """Write every position (incl. superseded) as JSONL, keyed by source ref —
    plus drops.jsonl (the scoreboard) beside it. With --ctx <dir>, writes the
    ctx decisions corpus instead: one markdown file per position, for `ctx` to
    index (hq-sl5k). The store stays the writer either way. --ctx and --out
    name two different outputs; passing both is a mistake, not a request to
    do both, so it's refused rather than silently picking one."""
    if ctx_dir is not None and out is not None:
        raise click.ClickException(
            "--ctx and --out write two different things (the ctx decisions corpus vs. "
            "the JSONL backup) — pass one or the other, not both"
        )
    if ctx_dir is not None:
        from . import ctx_export

        try:
            stats = ctx_export.export_ctx(ctx_dir)
        except ctx_export.TargetDirNotOwnedError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"exported {stats.written} positions -> {ctx_dir}")
        if stats.removed:
            click.echo(f"removed {len(stats.removed)} stale file(s): {', '.join(stats.removed)}")
        if stats.missing_summary:
            click.echo(
                f"{len(stats.missing_summary)} position(s) have no usable one-line "
                f"statement, so 'summary' was omitted (ctx lint will flag these): "
                f"{', '.join(stats.missing_summary)}"
            )
        return

    out = out or DEFAULT_EXPORT
    from .models import Drop, Metric

    db.init_db()
    out.parent.mkdir(parents=True, exist_ok=True)
    with db.session() as s, out.open("w") as f:
        rows = list(
            s.scalars(select(Position).order_by(Position.source_system, Position.source_ref))
        )
        for p in rows:
            f.write(json.dumps(_row(p), ensure_ascii=False) + "\n")
        drops = list(s.scalars(select(Drop).order_by(Drop.posted_on, Drop.id)))
        metrics = list(s.scalars(select(Metric).order_by(Metric.name)))
        mrows = [_metric_row(m) for m in metrics]
    with _drops_path(out).open("w") as f:
        for d in drops:
            f.write(json.dumps(_drop_row(d), ensure_ascii=False) + "\n")
    with _metrics_path(out).open("w") as f:
        for r in mrows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    click.echo(f"exported {len(rows)} positions → {out}")
    click.echo(f"exported {len(drops)} drops → {_drops_path(out)}")
    click.echo(f"exported {len(metrics)} metrics → {_metrics_path(out)}")


@main.command(name="import")
@click.option(
    "--src",
    "src_path",
    type=click.Path(path_type=Path, exists=True),
    default=DEFAULT_EXPORT,
    show_default=True,
)
def import_cmd(src_path: Path):
    """Restore/merge from a JSONL export. Store-owned columns come from the file."""
    db.init_db()
    rows = [json.loads(ln) for ln in src_path.read_text().splitlines() if ln.strip()]
    with db.session() as s:
        by_key: dict[str, Position] = {}
        n_new = 0
        for r in rows:
            sys_, ref = r["source"].split(":", 1)
            p = s.scalar(
                select(Position).where(Position.source_system == sys_, Position.source_ref == ref)
            )
            if p is None:
                p = Position(source_system=sys_, source_ref=ref, title=r["title"])
                s.add(p)
                n_new += 1
            p.title, p.statement = r["title"], r.get("statement") or ""
            p.element_key, p.status, p.scope, p.venture = (
                r.get("element_key"),
                r["status"],
                r["scope"],
                r.get("venture"),
            )
            p.source_status = r.get("source_status")
            p.decided_on = date.fromisoformat(r["decided_on"]) if r.get("decided_on") else None
            if not p.evidence:
                for e in r.get("evidence", []):
                    s.add(
                        Evidence(
                            position=p,
                            source=e["source"],
                            scope=e["scope"],
                            summary=e["summary"],
                            observed_on=date.fromisoformat(e["observed_on"])
                            if e.get("observed_on")
                            else None,
                        )
                    )
            by_key[r["source"]] = p
        s.flush()
        for r in rows:
            sup = r.get("supersedes")
            if not sup:
                continue
            for ref in [sup] if isinstance(sup, str) else sup:
                if ref in by_key:
                    by_key[ref].superseded_by_id = by_key[r["source"]].id
        s.commit()
    click.echo(f"imported {len(rows)} positions ({n_new} new) from {src_path}")
    dpath = _drops_path(src_path)
    if dpath.exists():
        from .drops import find_drop
        from .models import Drop

        drows = [json.loads(ln) for ln in dpath.read_text().splitlines() if ln.strip()]
        d_new = 0
        with db.session() as s:
            for r in drows:
                d = find_drop(s, r["url"], r.get("activity_id"), r.get("share_id"))
                if d is None:
                    d = Drop(url=r["url"], posted_on=date.fromisoformat(r["posted_on"]))
                    s.add(d)
                    d_new += 1
                for k, v in r.items():
                    if k in ("url", "posted_on"):
                        continue
                    if k == "stats_as_of":
                        v = date.fromisoformat(v) if v else None
                    setattr(d, k, v)
            s.commit()
        click.echo(f"imported {len(drows)} drops ({d_new} new) from {dpath}")
    mpath = _metrics_path(src_path)
    if mpath.exists():
        from .metrics import record
        from .models import Metric

        mrows = [json.loads(ln) for ln in mpath.read_text().splitlines() if ln.strip()]
        m_new = 0
        with db.session() as s:
            for r in mrows:
                m = s.scalar(select(Metric).where(Metric.name == r["name"]))
                if m is None:
                    m = Metric(name=r["name"])
                    s.add(m)
                    m_new += 1
                for k in ("title", "beat", "source", "source_key", "cadence", "gates", "position_id", "notes"):
                    setattr(m, k, r.get(k) if r.get(k) is not None else getattr(m, k))
                m.due_on = date.fromisoformat(r["due_on"]) if r.get("due_on") else None
                s.flush()
                for rd in r.get("readings", []):
                    record(s, m, rd["value"], date.fromisoformat(rd["as_of"]), rd.get("origin", ""), rd.get("note", ""))
            s.commit()
        click.echo(f"imported {len(mrows)} metrics ({m_new} new) from {mpath}")


# ------------------------------------------------------------------ forcing


# One label per drift class, so `forcing`, `reconcile` and `prime` cannot describe
# the same finding three different ways.
_DRIFT_LABEL = {
    beadlink.SHIPPED: "SHIPPED-BUT-GATED",
    beadlink.SETTLED: "SETTLED-BUT-GATED",
    beadlink.SUPERSEDED: "SUPERSEDED",
    beadlink.MISSING: "MISSING BEAD",
    beadlink.STALE: "premise unchecked",
}


def _drift_summary(findings) -> str:
    """One line. Says nothing found, rather than saying nothing."""
    if not findings:
        return "no gate drift: every linked bead is still open, and no dated gate is unlinked"
    counts = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    parts = [f"{counts[k]} {_DRIFT_LABEL[k]}" for k in _DRIFT_LABEL if k in counts]
    return " · ".join(parts) + "  — reported, never auto-resolved; `strategy gate resolve` is yours"


@main.command()
@click.option("--all", "show_all", is_flag=True, help="include resolved gates")
@click.option("--no-check", "no_check", is_flag=True,
              help="skip reading the rigs (offline, or when bd is slow)")
def forcing(show_all: bool, no_check: bool):
    """Dated gates, soonest first; undated gates last (they are the finding).

    Reads the rigs for every gate that names a satisfying bead and marks the
    disagreements INLINE, because that is the whole lesson: a drift report you
    have to remember to run reproduces the failure it was built to fix. The
    answer is cached for `prime`, which cannot afford the read itself.

    It never resolves anything. A closed bead is evidence a gate can close, not
    authority to close it — `gate resolve` is a person's verb."""
    with db.session() as s:
        rows = queries.gates(s, show_all=show_all)
        findings = [] if no_check else beadlink.check(s)
        by_gate: dict[int, list] = {}
        for f in findings:
            by_gate.setdefault(f.gate_id, []).append(f)
        for g in rows:
            r = g["row"]
            if g["undated"]:
                when = "  NO DATE "
            elif g["missed"]:
                when = f"{r.due_on} MISSED"
            else:
                when = f"{r.due_on} {g['days']:+4d}d"
            link = f" → pos #{r.position_id}" if r.position_id else ""
            mark = "  ⚠ DRIFT" if any(
                f.kind != beadlink.STALE for f in by_gate.get(r.id, [])
            ) else ""
            click.echo(f"{when}  [{r.venture or 'portfolio':<24}] {r.title}{link}{mark}")
            click.echo(f"{'':19}gates: {r.gates[:110]}")
            for f in by_gate.get(r.id, []):
                click.echo(f"{'':19}{_DRIFT_LABEL[f.kind]}: {f.detail[:150]}")
        click.echo(
            f"-- {len(rows)} gates, {sum(1 for g in rows if g['undated'])} without a date"
        )
        if no_check:
            click.echo("   (rigs not read — drop --no-check to check the linked beads)")
        else:
            click.echo("   " + _drift_summary(findings))
        from .forcing_seed import reseed_drift

        seedy = reseed_drift(s)
        if seedy:
            ids = ", ".join(f"#{d['row'].id}" for d in seedy)
            click.echo(
                f"   ⚠ {len(seedy)} gate(s) would be RE-DATED by the next `strategy "
                f"seed-forcing`: {ids} — the file owns the date and disagrees with the "
                f"store. `strategy reconcile` for the detail."
            )


@main.command(name="seed-forcing")
def seed_forcing_cmd():
    """Insert/refresh the hand-collected forcing rows (forcing_seed.py)."""
    from .forcing_seed import upsert_forcing

    db.init_db()
    with db.session() as s:
        c = upsert_forcing(s)
    click.echo(f"forcing: inserted {c['inserted']} · refreshed {c['updated']}")


# ------------------------------------------------------------------- report

DEFAULT_REPORT = DEFAULT_EXPORT.parent / "Playing_to_Win_Report.md"


@main.command()
@click.option("--framework", default="ptw", show_default=True)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help=f"summary path (default when --write: {DEFAULT_REPORT})",
)
@click.option("--write", is_flag=True, help="write summary + detail next to the JSONL export")
@click.option("--detail", "detail_only", is_flag=True, help="print the detail document instead")
def report(framework: str, out: Path | None, write: bool, detail_only: bool):
    """Render the cascade from the store: a concise summary, plus a detail companion."""
    from .report import render

    if write and out is None:
        out = DEFAULT_REPORT
    if out:
        detail_path = out.with_name(out.stem.replace("_Report", "") + "_Detail.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(framework, detail=False, detail_name=detail_path.name))
        detail_path.write_text(render(framework, detail=True))
        click.echo(f"wrote {out} ({len(out.read_text().splitlines())} lines) + {detail_path.name}")
    else:
        click.echo(render(framework, detail=detail_only))


@main.command()
@click.option("--title", required=True)
@click.option("--statement", default="")
@click.option("--element", default=None)
@click.option("--status", type=click.Choice(STATUSES), default="holding")
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    required=True,
    help="declare what the deciding session could see",
)
@click.option(
    "--source",
    "source_ref",
    required=True,
    help="where this came from, e.g. Playing_to_Win_Strategy.md#box1",
)
@click.option("--decided", "decided_on", type=click.DateTime(["%Y-%m-%d"]))
@click.option("--venture")
def add(title, statement, element, status, scope, source_ref, decided_on, venture):
    """Add a position by hand (source_system=doc or manual, chosen from --source)."""
    system = "doc" if ("." in source_ref or "/" in source_ref) else "manual"
    _check_lengths(Position, title=title, source_ref=source_ref, element_key=element,
                   venture=venture)
    with db.session() as s:
        p = Position(
            title=title,
            statement=statement,
            element_key=element,
            status=status,
            scope=scope,
            source_system=system,
            source_ref=source_ref,
            venture=venture,
            decided_on=decided_on.date() if decided_on else None,
        )
        s.add(p)
        s.commit()
        click.echo(_fmt(p, set()))


@main.command()
@click.option("--rig", default=None, help="override rig detection (default: from cwd under ~/gt)")
@click.option(
    "--seed-if-older-than",
    "hours",
    type=float,
    default=None,
    help="reseed from Vikunja+beads first if the newest sync is older than HOURS",
)
def prime(rig, hours):
    """Print the Direction block for session start. Never fails the session."""
    from .prime import detect_rig, maybe_reseed, render

    try:
        db.init_db()
        note = maybe_reseed(hours)
        click.echo(render(rig or detect_rig(), note))
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"## Direction (strategy store unavailable: {type(exc).__name__}: {str(exc)[:80]})"
        )


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="0.0.0.0 exposes it on the LAN — this surface has no auth of its own")
@click.option("--port", default=8021, show_default=True)
@click.option("--reload", is_flag=True, help="auto-reload on source change (development)")
def web(host: str, port: int, reload: bool):
    """Serve the admin surface — the CLI's read side as browsable pages.

    Read-only on purpose. Positions carry judgment and the invariants that
    protect them live in these command handlers; a second WRITER would have to
    call them rather than re-implement them (position #67), so writes stay here
    until that code moves. Browsing does not need to wait for it.

    The store it renders is whatever STRATEGY_DB_URL resolves to — nothing
    personal lives in the package. `strategy doctor` says which one that is.
    """
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - install-shape guidance
        raise click.ClickException(
            "the web surface needs its extra: `uv sync --extra web` in the repo, "
            "or `uv tool install '.[web]'`"
        ) from exc

    cfg = db.store()
    click.echo(f"strategy admin · {env.mask(cfg.value)} (from {cfg.origin})")
    click.echo(f"  http://{host}:{port}  — read-only")
    uvicorn.run(
        "strategy_store.web.app:app" if reload else _web_app(),
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


def _web_app():
    """Build the app, having first checked the store is actually readable.

    Deliberately NOT `init_db()`: creating or altering tables is a write, and a
    read-only surface has no business doing one. `strategy init` owns that."""
    from sqlalchemy import inspect

    from .web import create_app

    try:
        tables = inspect(db.engine()).get_table_names()
    except db.StoreUnreachable as exc:
        raise click.ClickException(str(exc)) from exc
    if "positions" not in tables:
        raise click.ClickException(
            f"no `positions` table in {env.mask(db.db_url())} — run `strategy init` "
            f"(this surface reads; it does not create schema)"
        )
    return create_app()


@main.command()
def doctor():
    """Which store am I on, where did each setting come from, how stale is it?

    The store is the thing you check a position against, so it has to be able to
    say what it is reading. Two resolution paths for one setting is what made
    `strategy prime` and `set -a; . ~/.env; set +a; strategy prime` two different
    programs (sea-mii7.3)."""
    from . import env
    from .prime import sync_ages

    db.reset()  # report what a new process would resolve, not a cached engine
    cfg = db.store()
    click.echo(f"store    {env.mask(cfg.value)}")
    click.echo(f"  from   {cfg.origin}")
    for f in env.ENV_FILES:
        click.echo(f"  .env   {f} {'' if f.exists() else '(absent)'}".rstrip())
    # A key dropped for a reference nothing defines reads exactly like a key
    # that was never set. Say which it is, here, where someone is looking.
    for problem in env.problems():
        click.echo(f"  {problem}")

    click.echo("\nsettings")
    for key in (
        "STRATEGY_VIKUNJA_PROJECTS",
        "STRATEGY_EXPORT_PATH",
        "STRATEGY_DOWNLOADS_DIR",
        "STRATEGY_FORCING_SEED",
        "STRATEGY_SEED_MAP",
        "GT_ROOT",
    ):
        r = env.resolve(key)
        shown = env.mask(r.value) if r.value else "—"
        click.echo(f"  {key:26s} {r.origin:14s} {shown[:70]}")

    try:
        db.init_db()
        with db.session() as sess:
            rows = list(sess.scalars(select(Position)))
    except db.StoreUnreachable as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"\nreachable · {len(rows)} positions")
    ages = sync_ages(rows)
    counts: dict[str, int] = {}
    for p in rows:
        counts[p.source_system] = counts.get(p.source_system, 0) + 1
    for src in sorted(counts):
        if src in ages:
            h = ages[src]
            flag = "  ← STALE" if h > 48 else ""
            click.echo(f"  {src:10s} {counts[src]:3d} rows · synced {h:5.1f}h ago{flag}")
        else:
            click.echo(f"  {src:10s} {counts[src]:3d} rows · hand-entered, no upstream")
    if ages and max(ages.values()) - min(ages.values()) > 12:
        click.echo(
            "  sources disagree by more than 12h — a reseed is refreshing only some of them"
        )


@main.group()
def gate():
    """Forcing gates: add one, correct one, or close one that has been answered.

    A gate with no way to close it is worse than no gate — it keeps asking for a
    decision that was already made, and a reader who has seen it be wrong once
    stops trusting the list."""


@gate.command(name="add")
@click.option("--title", required=True)
@click.option("--condition", required=True, help="checkable on a date, not a vibe")
@click.option("--gates", required=True, help="what the gate holds back")
@click.option("--due", "due_on", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--venture", default=None)
@click.option("--position", "position_id", type=int, default=None)
@click.option("--source", "source_ref", required=True)
@click.option("--satisfied-by", "satisfied_by", multiple=True, metavar="BEAD_ID",
              help="fully-qualified bead id whose closing would satisfy this gate; repeatable")
@click.option("--event", "event_note", default=None, metavar="TEXT",
              help="this gate is satisfied by something HAPPENING, not by a bead closing")
def gate_add(title, condition, gates, due_on, venture, position_id, source_ref,
             satisfied_by, event_note):
    """Add a forcing gate by hand. No --due is allowed, and is itself a finding.

    `--satisfied-by` names the WORK that would close it; `--event` says a human
    has to be there on the day. Neither is required, and giving neither leaves the
    gate UNCLASSIFIED — which is a visible state, not a default, because "nobody
    has looked" and "nothing could satisfy it" are different facts."""
    from .models import Forcing

    with db.session() as s:
        if position_id and not s.get(Position, position_id):
            raise click.ClickException(f"no position {position_id}")
        # Several gates may cite one session/source; keep the ref unique per title.
        taken = {
            r
            for (r,) in s.execute(
                select(Forcing.source_ref).where(Forcing.source_system == "manual")
            )
        }
        ref = source_ref
        if ref in taken:
            ref = f"{source_ref}:{title[:40]}"
        if ref in taken:
            raise click.ClickException(f"a manual gate with source {ref!r} already exists")
        f = Forcing(
            title=title,
            condition=condition,
            gates=gates,
            venture=venture,
            position_id=position_id,
            source_system="manual",
            source_ref=ref,
            due_on=due_on.date() if due_on else None,
        )
        s.add(f)
        s.flush()
        for line in _link_satisfiers(s, f, satisfied_by, event_note):
            click.echo(f"    {line}")
        s.commit()
        click.echo(f"gate #{f.id} {f.due_on or 'NO DATE'} {title}")


@gate.command(name="edit")
@click.argument("gate_id", type=int)
@click.option("--title")
@click.option("--condition", help="checkable on a date, not a vibe")
@click.option("--gates", "gates_text", help="what the gate holds back")
@click.option("--due", "due_on", type=click.DateTime(["%Y-%m-%d"]), default=None,
              help="move the date — needs --note")
@click.option("--clear-due", is_flag=True, help="remove the date — needs --note, "
                                                "and an undated gate is itself a finding")
@click.option("--venture")
@click.option("--position", "position_id", type=int)
@click.option("--note", help="why it changed; REQUIRED to move or clear a date")
def gate_edit(gate_id, title, condition, gates_text, due_on, clear_due,
              venture, position_id, note):
    """Correct a gate in place, without resolving and re-adding it.

    The verb that was missing: a gate whose PROSE is wrong — a stale date in the
    text, a counterparty that changed, a condition that reads as a vibe — used to
    need resolve-and-re-add, which rewrites a live deadline and buries the
    original under a resolution it never had. Correcting a mistake is not the
    same act as answering a question.

    Two refusals, both of them the point:

    * A gate the SEED FILE owns cannot be edited here. It would be overwritten on
      the next `strategy seed`, so the file is the place to change it.
    * Moving or clearing a date requires --note. The table exists because dated
      gates are what stop deferral without a trigger; a date that slides with no
      reason on the record is the failure this store was built to catch."""
    from .models import Forcing
    from .forcing_seed import seeded_refs

    if due_on and clear_due:
        raise click.ClickException("pass --due or --clear-due, not both")

    with db.session() as s:
        f = s.get(Forcing, gate_id)
        if f is None:
            raise click.ClickException(f"no gate {gate_id}")
        if f.resolved_on:
            raise click.ClickException(
                f"gate #{gate_id} was resolved on {f.resolved_on} — its text is the record "
                f"of a closed decision. `gate reopen {gate_id}` first if it is genuinely open."
            )
        if (f.source_system, f.source_ref) in seeded_refs():
            raise click.ClickException(
                f"gate #{gate_id} is seeded from the forcing-seed file "
                f"({f.source_system}:{f.source_ref}) — an edit here is overwritten on the "
                f"next `strategy seed`. Change it in the file."
            )
        if position_id and not s.get(Position, position_id):
            raise click.ClickException(f"no position {position_id}")

        new_due = due_on.date() if due_on else (None if clear_due else f.due_on)
        date_moves = new_due != f.due_on
        if date_moves and not note:
            raise click.ClickException(
                "moving a gate's date needs a reason on the record — pass --note"
            )

        wanted = {"title": title, "condition": condition, "gates": gates_text,
                  "venture": venture, "position_id": position_id}
        changed = {k: v for k, v in wanted.items() if v is not None and v != getattr(f, k)}
        if date_moves:
            changed["due_on"] = new_due
        if not changed:
            raise click.ClickException("nothing to change — every value given is already set")

        # The audit goes in `gates` beside the resolve notes, so one field carries
        # a gate's whole history and `strategy forcing` shows it without a second
        # lookup. Replaced text is quoted into the line rather than lost, since a
        # --gates rewrite would otherwise take the log with it.
        parts = []
        for k, v in changed.items():
            was = getattr(f, k)
            parts.append(f"{k}: {was!r} → {v!r}" if k in ("due_on", "venture", "position_id")
                         else f"{k} rewritten (was: {was!r})")
        line = f"\n[edited {date.today()}] " + "; ".join(parts) + (f" · {note}" if note else "")

        for k, v in changed.items():
            setattr(f, k, v)
        f.gates = (f.gates or "") + line
        s.commit()
        click.echo(f"edited #{f.id}  {f.title}")
        for part in parts:
            click.echo(f"    {part}")
        if date_moves:
            click.echo(f"    now due: {f.due_on or 'NO DATE'}")


def _link_satisfiers(session, gate, bead_ids, event_note, position_id=None, predicate=None):
    """Attach satisfiers to a gate. Returns one line per row for the caller to echo.

    Shared by `gate add` and `gate link` so the two can never disagree about what
    a link is — the mistake `record()` made twice over in sea-zrbp.
    """
    from .models import GateSatisfier

    lines = []
    existing = {sat.bead_id for sat in gate.satisfiers if sat.bead_id}
    for bead_id in bead_ids:
        bead_id = bead_id.strip()
        if not bead_id:
            continue
        if bead_id in existing:
            lines.append(f"already linked: {bead_id}")
            continue
        state = beadlink.read_state(bead_id)
        if state is None:
            # Refuse rather than store it. A bead id no rig recognises reads as
            # "not closed yet" forever, so the gate looks watched and is not.
            raise click.ClickException(
                f"no rig has a bead {bead_id!r} — searched "
                f"{', '.join(beadlink.rig_candidates(bead_id)) or '(no rigs found)'}. "
                f"Use the fully-qualified id; gates are cross-rig by design."
            )
        session.add(GateSatisfier(forcing_id=gate.id, kind="bead", bead_id=bead_id))
        existing.add(bead_id)
        flag = f" — ALREADY {state.status.upper()}" if state.done else ""
        lines.append(f"satisfied-by {bead_id} [{state.rig}] {state.title[:60]}{flag}")
    if position_id is not None:
        from .models import Position

        pos = session.get(Position, position_id)
        if pos is None:
            raise click.ClickException(f"no position {position_id}")
        already = {
            (sat.position_id, sat.predicate) for sat in gate.satisfiers if sat.kind == "position"
        }
        if (position_id, predicate) in already:
            lines.append(f"already linked: position #{position_id} {predicate}")
        else:
            session.add(
                GateSatisfier(
                    forcing_id=gate.id, kind="position",
                    position_id=position_id, predicate=predicate,
                )
            )
            done = (
                pos.superseded_by_id if predicate == "superseded" else pos.decided_on
            )
            flag = f" — ALREADY {predicate.upper()}" if done else ""
            lines.append(
                f"satisfied-when position #{position_id} is {predicate}: "
                f"{pos.title[:55]}{flag}"
            )
    if event_note:
        notes = {sat.note for sat in gate.satisfiers if sat.kind == "event"}
        if event_note in notes:
            lines.append(f"already recorded as event-satisfiable: {event_note}")
        else:
            session.add(GateSatisfier(forcing_id=gate.id, kind="event", note=event_note))
            lines.append(f"event-satisfiable: {event_note}")
    return lines


@gate.command(name="link")
@click.argument("gate_id", type=int)
@click.argument("bead_ids", nargs=-1)
@click.option("--event", "event_note", default=None, metavar="TEXT",
              help="record that an EVENT satisfies this gate, not a bead")
@click.option("--position", "position_id", type=int, default=None, metavar="N",
              help="this gate closes when position #N moves; needs --when")
@click.option("--when", "predicate", type=click.Choice(POSITION_PREDICATES), default=None,
              help="what has to become true of --position: superseded | decided")
@click.option("--historical", is_flag=True,
              help="permit a RESOLVED gate — records what satisfied it, for the record")
def gate_link(gate_id, bead_ids, event_note, position_id, predicate, historical):
    """Name the work that would satisfy a gate — cross-rig, fully-qualified ids.

    This is not `--source`. `source_ref` is PROVENANCE: where the gate came from.
    A link is SATISFACTION: what would make it stop asking. They are routinely
    different rows in different systems, and conflating them is why gate #6 held
    back the QR handout for eight weeks after sea-btz.6 closed.

    An id no rig recognises is REFUSED, not stored — a typo'd link reads as "not
    closed yet" forever, so the gate looks watched and is not.

    `--position N --when superseded|decided` records the third answer, and the
    cheapest one: this gate closes when a claim already in this store moves. Gate
    #20's "a position exists that supersedes #22" is that, asked of #22. It costs
    one local SELECT, so unlike a bead link it can be checked on every session.

    `--event` records the other answer: this gate closes because something
    HAPPENS. A room either sold seats or it did not, and no bead closing proves
    it. Only bead links can be auto-checked; an event still needs a human on the
    day, and saying so is the point of recording it.

    A RESOLVED gate is refused unless you pass `--historical`, which is the
    backfill case and not the normal one. Half this table is already resolved, so
    a blanket refusal would make the closed half permanently unclassifiable; but
    the default has to be no, because attaching work to an answered question
    quietly implies that work is why it closed. `--historical` makes the claim
    deliberate. It is inert either way — a resolved gate is never reported as
    drift, whatever it links to."""
    from .models import Forcing

    if not bead_ids and not event_note and position_id is None:
        raise click.ClickException("give at least one bead id, or --event, or --position")
    if (position_id is None) != (predicate is None):
        raise click.ClickException(
            "--position and --when go together: a position with no predicate names a row "
            "without saying what has to become true of it, which nothing can evaluate"
        )

    with db.session() as s:
        f = s.get(Forcing, gate_id)
        if f is None:
            raise click.ClickException(f"no gate {gate_id}")
        if f.resolved_on and not historical:
            raise click.ClickException(
                f"gate #{gate_id} was resolved on {f.resolved_on} — linking work to a "
                f"closed gate implies that work is why it closed. Pass --historical to "
                f"record it anyway (the backfill case), or `gate reopen {gate_id}` if the "
                f"question is genuinely still open."
            )
        lines = _link_satisfiers(s, f, bead_ids, event_note, position_id, predicate)
        s.commit()
        click.echo(f"#{f.id} {f.title[:70]}")
        for line in lines:
            click.echo(f"    {line}")


@gate.command(name="unlink")
@click.argument("gate_id", type=int)
@click.argument("bead_ids", nargs=-1, required=True)
def gate_unlink(gate_id, bead_ids):
    """Remove a satisfier link. The gate and the bead are both untouched."""
    from .models import Forcing

    with db.session() as s:
        f = s.get(Forcing, gate_id)
        if f is None:
            raise click.ClickException(f"no gate {gate_id}")
        wanted = set(bead_ids)
        gone = [sat for sat in f.satisfiers if sat.bead_id in wanted]
        if not gone:
            raise click.ClickException(
                f"gate #{gate_id} has no link to {', '.join(sorted(wanted))}"
            )
        for sat in gone:
            f.satisfiers.remove(sat)
            click.echo(f"unlinked #{gate_id} ✗ {sat.bead_id}")
        s.commit()


@gate.command(name="satisfiers")
@click.option("--all", "show_all", is_flag=True, help="include resolved gates")
@click.option("--check", is_flag=True,
              help="read each linked bead and flag a closed one under an open gate")
def gate_satisfiers(show_all, check):
    """Every gate, and what would satisfy it: a bead, an event, or nothing yet.

    The three-way split is the finding this exists to produce. Only BEAD gates can
    be reconciled automatically. EVENT gates cannot, ever — an event happening is
    not a bead closing — and calling them checkable would manufacture a green
    light on a Tuesday nobody was watching. UNCLASSIFIED means nobody has looked,
    which is neither of the above and must not be read as either.

    `--check` reads the rigs. It reports and resolves nothing: a closed bead is
    evidence a gate can close, not authority to close it."""
    from sqlalchemy.orm import selectinload

    from .models import Forcing

    with db.session() as s:
        q = select(Forcing).options(selectinload(Forcing.satisfiers)).order_by(Forcing.id)
        if not show_all:
            q = q.where(Forcing.resolved_on.is_(None))
        gates = s.execute(q).scalars().all()

        # Keyed off classify()'s own vocabulary, so a new kind cannot be added to
        # the model and silently fall out of this report (it did, once).
        order = (beadlink.BEAD, beadlink.POSITION, beadlink.MIXED,
                 beadlink.EVENT, beadlink.UNCLASSIFIED)
        buckets = {k: [] for k in order}
        for g in gates:
            buckets.setdefault(beadlink.classify(g.satisfiers), []).append(g)

        for label in buckets:
            rows = buckets[label]
            if not rows:
                continue
            click.echo(f"\n{label.upper()} ({len(rows)})")
            for g in rows:
                due = g.due_on.isoformat() if g.due_on else "NO DATE"
                res = f" [resolved {g.resolved_on}]" if g.resolved_on else ""
                click.echo(f"  #{g.id:<3} {due:<11}{res} {g.title[:66]}")
                for sat in sorted(g.satisfiers, key=lambda x: (x.kind, x.bead_id or "")):
                    if sat.kind == "event":
                        click.echo(f"        event: {sat.note[:80]}")
                        continue
                    if sat.kind == "position":
                        click.echo(
                            f"        pos:   #{sat.position_id} {sat.predicate}"
                        )
                        continue
                    if not check:
                        click.echo(f"        bead:  {sat.bead_id}")
                        continue
                    st = beadlink.read_state(sat.bead_id)
                    if st is None:
                        click.echo(f"        bead:  {sat.bead_id}  ← NO RIG HAS THIS BEAD")
                    elif st.done and not g.resolved_on:
                        age = (date.today() - st.closed_at).days if st.closed_at else None
                        stale = f", {age}d ago" if age is not None else ""
                        click.echo(
                            f"        bead:  {sat.bead_id} [{st.rig}] {st.status.upper()}"
                            f"{stale}  ← DRIFT: gate is open"
                        )
                    else:
                        click.echo(f"        bead:  {sat.bead_id} [{st.rig}] {st.status}")

        click.echo(
            f"\n-- {len(gates)} gates: "
            + ", ".join(f"{len(v)} {k}" for k, v in buckets.items())
        )
        click.echo(
            "   position is checked from this store alone; bead needs a rig read; "
            "event never can be — a human is there on the day, or nobody is; "
            "unclassified means nobody has looked"
        )


@gate.command(name="resolve")
@click.argument("gate_ids", type=int, nargs=-1, required=True)
@click.option("--on", "resolved_on", type=click.DateTime(["%Y-%m-%d"]), default=None,
              help="date it was answered (default: today)")
@click.option("--note", help="what answered it — a position number, a decision, an event")
def gate_resolve(gate_ids, resolved_on, note):
    """Close a gate that has been answered, so it stops asking.

    A gate is resolved when the thing it was holding back can proceed — not when
    it stops being interesting. Resolving one whose question is still open is the
    failure this command makes easy, so it prints what each gate was holding back
    and expects the caller to have read it."""
    from .models import Forcing

    when = resolved_on.date() if resolved_on else date.today()
    with db.session() as s:
        rows = [s.get(Forcing, g) for g in gate_ids]
        for g, f in zip(gate_ids, rows):
            if f is None:
                raise click.ClickException(f"no gate {g}")
            if f.resolved_on:
                raise click.ClickException(
                    f"gate #{g} was already resolved on {f.resolved_on} — {f.title}"
                )
        for f in rows:
            f.resolved_on = when
            if note:
                f.gates = f"{f.gates}\n[resolved {when}] {note}"
            click.echo(f"resolved #{f.id} {when}  {f.title}")
            click.echo(f"    was holding: {f.gates.splitlines()[0][:140]}")
        s.commit()


@gate.command(name="reopen")
@click.argument("gate_ids", type=int, nargs=-1, required=True)
def gate_reopen(gate_ids):
    """Undo a resolve — the question turned out still to be open."""
    from .models import Forcing

    with db.session() as s:
        for g in gate_ids:
            f = s.get(Forcing, g)
            if f is None:
                raise click.ClickException(f"no gate {g}")
            f.resolved_on = None
            click.echo(f"reopened #{f.id}  {f.title}")
        s.commit()


# ------------------------------------------------------------------- drops


# ------------------------------------------------------------------ metrics


@main.group()
def metric():
    """The numbers being chased, per beat — readings as a time series, goals with gates."""
    db.init_db()


@metric.command(name="add")
@click.option("--name", required=True, help="short handle: li-newsletter-subs, bd-method …")
@click.option("--title", default="")
@click.option("--beat", default="", help="ivantohelpyou-linkedin | mcd-linkedin-page | mcd-buttondown | …")
@click.option("--source", type=click.Choice(["buttondown-api", "linkedin-export", "page_views", "manual"]), default="manual")
@click.option("--source-key", default="", help="buttondown-api: env var with the token · linkedin-export: export column (email_sends)")
@click.option("--cadence", default="weekly")
@click.option("--gates", default="", help="ascending, comma-separated: 300,600,1000 — makes it a goal")
@click.option("--due", "due_on", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--position", "position_id", type=int, default=None, help="position id stating the goal")
@click.option("--note", default="")
def metric_add(name, title, beat, source, source_key, cadence, gates, due_on, position_id, note):
    """Register a metric (re-run on the same name to update the fields you pass)."""
    from .models import Metric

    with db.session() as s:
        m = s.scalar(select(Metric).where(Metric.name == name))
        if m is None:
            m = Metric(name=name)
            s.add(m)
        for k, v in dict(title=title, beat=beat, source_key=source_key, gates=gates, notes=note).items():
            if v:
                setattr(m, k, v)
        m.source = source if source != "manual" or not m.source else m.source
        if cadence != "weekly" or not m.cadence:
            m.cadence = cadence
        if due_on:
            m.due_on = due_on.date()
        if position_id is not None:
            m.position_id = position_id
        s.commit()
        click.echo(f"metric #{m.id} {m.name} [{m.beat}] {m.source} gates={m.gates or '-'}")


@metric.command(name="read")
@click.argument("name")
@click.argument("value", type=int)
@click.option("--on", "as_of", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--note", default="")
def metric_read(name, value, as_of, note):
    """Record a reading by hand (one per metric per day; re-run to correct)."""
    from .metrics import record
    from .models import Metric

    with db.session() as s:
        m = s.scalar(select(Metric).where(Metric.name == name))
        if m is None:
            raise click.ClickException(f"no metric {name} — `strategy metric add --name {name} …` first")
        r, created = record(s, m, value, as_of.date() if as_of else None, origin="ivan", note=note)
        click.echo(f"{'new' if created else 'upd'} {m.name} {r.as_of} = {r.value}")


@metric.command(name="pull")
def metric_pull():
    """Read every API-sourced metric (Buttondown) and record today's value."""
    from .metrics import pull

    with db.session() as s:
        for m, v, msg in pull(s):
            click.echo(f"  {m.name:<22} {v if v is not None else '-':>6}  {msg}")


@metric.command(name="list")
@click.option("--readings", is_flag=True, help="show the last 5 readings per metric")
def metric_list(readings):
    """Latest value, 7-day delta and next gate per metric."""
    from .metrics import delta
    from .models import Metric

    with db.session() as s:
        ms = list(s.scalars(select(Metric).order_by(Metric.beat, Metric.name)))
        for m in ms:
            last = m.latest
            d = delta(m)
            click.echo(
                f"{m.name:<22} {m.beat:<22} {m.source:<16} "
                f"{(last.value if last else '-'):>6}  {('as of ' + str(last.as_of)) if last else 'no reading':<16} "
                f"{(f'{d[0]:+d} since {d[1]}' if d is not None else ''):<22} "
                f"{('→ ' + str(m.next_gate())) if m.gate_list and m.next_gate() else ''}"
            )
            if readings:
                for r in m.readings[-5:]:
                    click.echo(f"    {r.as_of}  {r.value:>6}  {r.origin}  {r.note}")
        if not ms:
            click.echo("no metrics — `strategy metric add --name … --source …`")


def fmt_duration(seconds: int) -> str:
    """Seconds back to LinkedIn's own display shape: 1114 -> "18m34s", 19 -> "19s"."""
    h, rem = divmod(int(seconds), 3600)
    m, sec = divmod(rem, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if h or m else "") + f"{sec}s"


@main.group()
def drop():
    """The daily-drop scoreboard: log drops, ingest LinkedIn exports, see if you got on base."""
    db.init_db()


@drop.command(name="log")
@click.option("--url", required=True, help="the LinkedIn post/article URL")
@click.option("--posted", "posted_on", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--title", default="")
@click.option(
    "--format",
    "fmt",
    default=None,
    type=click.Choice(["article", "post", "carousel", "video", "comment-link"]),
    help="what the media IS; default post. 'comment-link' is accepted for muscle "
    "memory but it is a placement, not a medium — it sets --link first-comment "
    "and leaves the medium alone.",
)
@click.option(
    "--link",
    "link_placement",
    default=None,
    type=click.Choice(["body", "first-comment", "none"]),
    help="where the app link lives. first-comment posts pay a reach-vs-clicks "
    "tradeoff worth measuring separately from the medium.",
)
@click.option("--app", "app_url", default=None, help="the live app this drop carries")
@click.option(
    "--own-comments",
    type=int,
    default=None,
    help="my own comments on the post (link-in-comments, replies) — not engagement; "
    "default 1 for comment-link, else 0",
)
@click.option(
    "--own-reposts",
    type=int,
    default=None,
    help="my own reposts of the post (the main-feed share of a page post) — not engagement",
)
@click.option("--note", default="")
def drop_log(url, posted_on, title, fmt, link_placement, app_url, own_comments, own_reposts, note):
    """Register a drop the moment it is posted (stats come later). Re-running on
    the same URL updates only the fields you pass."""
    from .drops import find_drop, ids_from_url, normalize_url, set_ids
    from .models import Drop

    # `--format comment-link` predates the medium/placement split (2026-08-24).
    # Honour it as the placement it always was, so old habits and old scripts
    # keep working — but never let it overwrite the medium.
    if fmt == "comment-link":
        fmt, link_placement = None, (link_placement or "first-comment")
    url = normalize_url(url)
    with db.session() as s:
        d = find_drop(s, url)
        if d is None:
            d = Drop(url=url, posted_on=(posted_on or datetime.now()).date(), format=fmt or "post")
            s.add(d)
        elif fmt:
            d.format = fmt
        if link_placement:
            d.link_placement = link_placement
        if d.url != url:
            # Same post under its other LinkedIn id (activity vs share URL form):
            # one drop, the first-logged URL stays.
            form = "activity" if ids_from_url(url)["activity_id"] else "share"
            click.echo(f"  note: {form}-form URL matches drop #{d.id} by id — keeping {d.url}")
        ids = ids_from_url(url)
        set_ids(d, ids["activity_id"], ids["share_id"])
        if own_comments is not None:
            d.own_comments = own_comments
        elif d.link_placement == "first-comment" and not d.own_comments:
            d.own_comments = 1  # the link comment is mine; it is not engagement
        if own_reposts is not None:
            d.own_reposts = own_reposts
        d.title, d.app_url, d.notes = (
            title or d.title,
            app_url or d.app_url,
            note or d.notes,
        )
        s.commit()
        own = f" own-comments={d.own_comments}" if d.own_comments else ""
        own += f" own-reposts={d.own_reposts}" if d.own_reposts else ""
        place = f" link={d.link_placement}" if d.link_placement else ""
        click.echo(f"drop #{d.id} {d.posted_on} {d.format}{place}{own} {d.url}")


@drop.command(name="ingest")
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path, exists=True))
@click.option(
    "--downloads",
    type=click.Path(path_type=Path),
    default=None,
    help="ingest every SinglePostAnalytics_*.xlsx in this folder",
)
@click.option(
    "--archive",
    "archive_dir",
    type=click.Path(path_type=Path),
    default=None,
    is_flag=False,
    flag_value=str(DEFAULT_DROPS_ARCHIVE),
    help="after ingesting, move each export here (the raw per-day record; the store keeps only "
    f"the latest numbers). Bare --archive = {DEFAULT_DROPS_ARCHIVE}",
)
def drop_ingest(paths, downloads, archive_dir):
    """Ingest LinkedIn single-post analytics exports (xlsx)."""
    import shutil

    from .drops import upsert_from_export

    def archive(f: Path) -> None:
        if archive_dir is None:
            return
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f.name
        if dest.exists():  # same name re-exported on another day: keep both, dated
            stamp = date.fromtimestamp(f.stat().st_mtime).isoformat()
            dest = archive_dir / f"{f.stem} [{stamp}]{f.suffix}"
        shutil.move(str(f), str(dest))
        click.echo(f"      → archived {dest.name}")

    files = list(paths)
    if downloads:
        # oldest export first, so a re-export (`X (1).xlsx`) lands last and wins
        files += sorted(
            Path(downloads).glob("SinglePostAnalytics_*.xlsx"), key=lambda p: p.stat().st_mtime
        )
    if not files:
        raise click.ClickException("nothing to ingest — pass files or --downloads DIR")
    with db.session() as s:
        for f in files:
            try:
                d, created = upsert_from_export(s, f)
            except Exception as exc:  # noqa: BLE001 — keep going through the folder
                click.echo(f"  skip {f.name}: {exc}")
                continue
            hit = {True: "ON BASE", False: "out", None: "?"}[d.base_hit]
            if d.email_sends is not None and d.stats_as_of:
                from .metrics import record_from_export

                for m in record_from_export(s, "email_sends", d.email_sends, d.stats_as_of, f"export:{f.name}"):
                    click.echo(f"      → {m.name} {d.stats_as_of} = {d.email_sends}")
            if not created and d.stats_as_of and d.stats_as_of > date.fromtimestamp(f.stat().st_mtime):
                click.echo(f"  old {f.name}: newer stats already stored for #{d.id}")
                archive(f)
                continue
            click.echo(
                f"  {'new' if created else 'upd'} #{d.id} {d.posted_on} {d.format:<8} "
                f"imp={d.impressions} clicks={d.link_clicks} follows={d.followers_gained} → {hit}"
            )
            archive(f)


@drop.command(name="backfill-ids")
@click.option(
    "--archive",
    "archive_dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_DROPS_ARCHIVE,
    show_default=True,
    help="archived exports to read ids from (filename = activity id, Post URL = share id)",
)
def drop_backfill_ids(archive_dir):
    """Fill activity_id/share_id on drops logged before the columns existed, from
    each drop's URL and from the archived exports. Idempotent."""
    from .drops import backfill_ids
    from .models import Drop

    with db.session() as s:
        touched = backfill_ids(s, archive_dir)
        for d, changed in touched:
            click.echo(f"  #{d.id} {', '.join(f'{c}={getattr(d, c)}' for c in changed)}")
        missing = [d for d in s.scalars(select(Drop)) if not (d.activity_id or d.share_id)]
    click.echo(f"backfilled {len(touched)} drops; {len(missing)} with no id"
               + (": " + ", ".join(f"#{d.id}" for d in missing) if missing else ""))


@drop.command(name="list")
@click.option("--days", default=30, show_default=True)
def drop_list(days):
    """Recent drops with the base-hit call, plus what needs stats."""
    from .drops import needs_stats, scoreboard
    from .models import Drop

    with db.session() as s:
        drops = list(s.scalars(select(Drop).order_by(Drop.posted_on.desc())))
        today = date.today()
        for d in drops:
            if (today - d.posted_on).days > days:
                continue
            hit = {True: "ON BASE", False: "out", None: "no stats"}[d.base_hit]
            clicks = "-" if d.link_clicks is None else d.link_clicks
            follows = "-" if d.followers_gained is None else d.followers_gained
            cmt = "-" if d.others_comments is None else d.others_comments
            own = f"(+{d.own_comments} mine)" if d.own_comments else ""
            rp = "-" if d.others_reposts is None else d.others_reposts
            own_rp = f"(+{d.own_reposts} mine)" if d.own_reposts else ""
            # Placement rides on the medium as a suffix rather than its own
            # column: it is only ever interesting next to what the post was.
            place = {"first-comment": "·fc", "body": "·body", "none": ""}.get(
                d.link_placement or "", ""
            )
            medium = f"{d.format}{place}"
            # The video block only exists on video posts; appending it keeps
            # every other row's columns aligned.
            vid = ""
            if d.video_views is not None:
                vid = f"  ▶ {d.video_views} views"
                if d.avg_watch_seconds:
                    vid += f" · {fmt_duration(d.avg_watch_seconds)} avg"
                if d.watch_seconds:
                    vid += f" · {fmt_duration(d.watch_seconds)} total"
            click.echo(
                f"#{d.id:<3}{d.posted_on} {medium:<13} imp={d.impressions or '-':>4} "
                f"reach={d.reached or '-':>4} cmt={cmt:>2}{own:<10} rp={rp:>2}{own_rp:<10} "
                f"clicks={clicks:>2} follows={follows:>2}  {hit:<8} {d.title[:50]}{vid}"
            )
        sb = scoreboard(drops)
        click.echo(
            f"-- last {sb['days']}d: dropped {sb['dropped']}, scored {sb['scored']}, "
            f"on base {sb['on_base']} · followers gained all-time {sb['followers']} "
            f"· last hit {sb['last_hit'] or 'never'}"
        )
        for d, why in needs_stats(drops):
            click.echo(f"   DOWNLOAD STATS: {d.url}  ({why})")


@drop.command(name="funnel")
@click.option("--days", default=30, show_default=True, help="drops posted within N days")
@click.option("--id", "drop_id", type=int, default=None, help="one drop by id")
def drop_funnel(days, drop_id):
    """LinkedIn clicks → landing views → in-app events (purr tables/hands; Heads in Beds eras/full trip), per drop, from the page_views plane."""
    from .funnel import analytics_url, funnel_for
    from .models import Drop

    if not analytics_url():
        raise click.ClickException(
            "no analytics DB: set STRATEGY_ANALYTICS_URL / PAGEVIEWS_DB_URL / "
            "QRCARDS_RUNTIME_DB_URL (or put one in ~/.env)"
        )
    with db.session() as s:
        drops = list(s.scalars(select(Drop).order_by(Drop.posted_on.desc())))
    today = date.today()
    for d in drops:
        if drop_id and d.id != drop_id:
            continue
        if not drop_id and (today - d.posted_on).days > days:
            continue
        if not d.app_url:
            continue
        f = funnel_for(d.app_url, d.posted_on)
        clicks = "-" if d.link_clicks is None else d.link_clicks
        click.echo(f"#{d.id} {d.posted_on} {d.title[:50]}  →  {f['domain']}")
        h = f["hib"]
        hib_played = any(h[k] for k in ("start", "era2", "era3", "complete"))
        if hib_played:
            # Heads in Beds: the full trip is start → era2 → era3 → complete
            def _pct(s):
                return s[4:] + "%" if s and s.startswith("lit-") else (s or "?")

            sc = ", ".join(
                f"{_pct(k)}×{n}" for k, n in sorted(h["scores"].items(), key=lambda kv: -kv[1])
            )
            click.echo(
                f"   LinkedIn clicks {clicks} → landing views {f['landing_views']} "
                f"({f['landing_from_linkedin']} from linkedin) → opened the doors {h['start']} "
                f"→ era 2 {h['era2']} → era 3 {h['era3']} → full trip {h['complete']}"
                + (f"  (lit: {sc})" if sc else "")
            )
        else:
            click.echo(
                f"   LinkedIn clicks {clicks} → landing views {f['landing_views']} "
                f"({f['landing_from_linkedin']} from linkedin) → tables {f['tables_created']} "
                f"→ joined {f['joined']} → dealt {f['dealt']} → completed {f['completed']}"
            )


@drop.command(name="stats")
@click.argument("drop_id", type=int)
@click.option("--impressions", type=int, default=None)
@click.option("--reached", type=int, default=None)
@click.option("--reactions", type=int, default=None)
@click.option(
    "--comments", type=int, default=None, help="total incl. mine (see --own-comments on log)"
)
@click.option(
    "--reposts", type=int, default=None, help="total incl. mine (see --own-reposts on log)"
)
@click.option("--clicks", "link_clicks", type=int, default=None)
@click.option("--follows", "followers_gained", type=int, default=None)
@click.option("--as-of", "as_of", type=click.DateTime(["%Y-%m-%d"]), default=None)
@click.option("--source", default="", help="where the numbers were read (e.g. 'MCD page admin UI')")
def drop_stats(drop_id, as_of, source, **stats):
    """Enter stats by hand for a drop that has no single-post export (LinkedIn page
    posts only show numbers in the page admin UI). Only the fields passed change;
    stats_as_of is set so the drop scores. Ingest still wins later if an export
    is newer."""
    from .models import Drop

    given = {k: v for k, v in stats.items() if v is not None}
    if not given:
        raise click.ClickException("pass at least one stat")
    with db.session() as s:
        d = s.get(Drop, drop_id)
        if not d:
            raise click.ClickException(f"no drop {drop_id}")
        for k, v in given.items():
            setattr(d, k, v)
        d.stats_as_of = (as_of or datetime.now()).date()
        stamp = d.stats_as_of.isoformat()
        what = ", ".join(f"{k}={v}" for k, v in given.items())
        src = f" from {source}" if source else ""
        d.notes = (d.notes + "\n" if d.notes else "") + f"[{stamp}] stats by hand{src}: {what}"
        s.commit()
        hit = {True: "ON BASE", False: "out", None: "no stats"}[d.base_hit]
        click.echo(f"drop #{d.id}: {what} (as of {stamp}) → {hit}")


@drop.command(name="note")
@click.argument("drop_id", type=int)
@click.argument("text")
def drop_note(drop_id, text):
    """Append a dated note to a drop (engagement, replies, what happened)."""
    from .models import Drop

    with db.session() as s:
        d = s.get(Drop, drop_id)
        if not d:
            raise click.ClickException(f"no drop {drop_id}")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        d.notes = (d.notes + "\n" if d.notes else "") + f"[{stamp}] {text}"
        s.commit()
        click.echo(f"drop #{d.id}: note added")


# ------------------------------------------------------------------ cascades


@main.group()
def cascade():
    """Cascades — the same five boxes, held from different points of view.

    A cascade is what Playing to Win calls the whole chain: aspiration, where to
    play, how to win, capabilities, management systems. The store held exactly
    one, implicitly — "every position with a box key" — which is fine until the
    portfolio has brands that each answer boxes 1-3 differently, or until you
    want to ask the generative question: hold capabilities and management
    systems constant, and see what where-to-play options the floor can carry.

    Cascades share positions rather than copying them, so a capability three
    brands rest on stays one row. `cascade shared` is that fact, reported.
    """


@cascade.command("create")
@click.argument("key")
@click.option("--name", default="", help="display name; defaults to the key")
@click.option("--brand", default=None, help="MCD | CCS | I2HU | hobby (free text)")
@click.option("--standing", type=click.Choice(CASCADE_STANDINGS), default="candidate",
              show_default=True, help="'actual' is what prime prints — promote deliberately")
@click.option("--direction", type=click.Choice(CASCADE_DIRECTIONS), default="forward",
              show_default=True, help="forward = 1->5 aspiration-led; reverse = 5->1 capability-led")
@click.option("--question", default="", help="the point of view, in one line (required for candidates)")
def cascade_create(key, name, brand, standing, direction, question):
    """Create a cascade. Candidates need a --question; actuals must be promoted."""
    with db.session() as s:
        try:
            c = core.create_cascade(
                s, key, name=name, brand=brand, standing=standing,
                direction=direction, question=question,
            )
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"cascade {c.key}: {c.standing}/{c.direction}"
                   + (f" · brand {c.brand}" if c.brand else ""))


@cascade.command("list")
@click.option("--standing", type=click.Choice(CASCADE_STANDINGS), help="filter")
@click.option("--all", "show_all", is_flag=True, help="include retired cascades")
def cascade_list(standing, show_all):
    """Every cascade, actuals first."""
    from .models import Cascade, CascadePosition

    with db.session() as s:
        q = select(Cascade)
        if standing:
            q = q.where(Cascade.standing == standing)
        rows = list(s.scalars(q))
        if not show_all:
            rows = [c for c in rows if c.retired_on is None]
        if not rows:
            click.echo("no cascades yet — `strategy cascade create <key>`")
            return
        counts = dict(
            s.execute(
                select(CascadePosition.cascade_id, func.count())
                .group_by(CascadePosition.cascade_id)
            ).all()
        )
        rows.sort(key=lambda c: (c.standing != "actual", c.brand or "~", c.key))
        for c in rows:
            mark = "●" if c.standing == "actual" else "○"
            tail = f" ← {s.get(Cascade, c.forked_from_id).key}" if c.forked_from_id else ""
            retired = "  [retired]" if c.retired_on else ""
            click.echo(
                f"{mark} {c.key:<24} {(c.brand or '—'):<6} {c.direction:<7} "
                f"{counts.get(c.id, 0):>3} positions{tail}{retired}"
            )
            if c.question:
                click.echo(f"    {c.question}")


@cascade.command("show")
@click.argument("key")
def cascade_show(key):
    """One cascade as five boxes. Empty boxes are printed — they are the finding."""
    from .frameworks import FRAMEWORKS

    with db.session() as s:
        try:
            c = core.cascade(s, key)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        boxes = core.cascade_boxes(s, c)
        head = f"{c.name} ({c.key}) — {c.standing}, {c.direction}"
        click.echo(head)
        click.echo("=" * len(head))
        if c.brand:
            click.echo(f"brand: {c.brand}")
        if c.question:
            click.echo(f"asks: {c.question}")
        if c.standing == "candidate":
            click.echo("(candidate — not shown in prime or report)")
        click.echo()
        for ekey, _ord, ename, _req, prompt in FRAMEWORKS[c.framework]["elements"]:
            members = boxes.get(ekey, [])
            click.echo(f"{ekey}  {ename}")
            if not members:
                click.echo(f"    — empty — {prompt}")
            for r in members:
                m, p = r["membership"], r["position"]
                flag = {"fixed": "▪", "open": "?", "derived": "→"}[m.mode]
                click.echo(f"    {flag} {m.mode:<8} #{p.id} {p.title[:66]}")
                if m.note:
                    click.echo(f"      {m.note}")
            click.echo()


@cascade.command("add")
@click.argument("key")
@click.argument("pids", type=int, nargs=-1, required=True)
@click.option("--mode", type=click.Choice(MEMBERSHIP_MODES), default="fixed", show_default=True)
@click.option("--element", "element_key", default=None,
              help="slot into a different box than the position's own")
@click.option("--note", default="")
def cascade_add(key, pids, mode, element_key, note):
    """Put positions in a cascade. All of them, or none."""
    with db.session() as s:
        try:
            c = core.cascade(s, key)
            targets = core.positions(s, pids)
            for p in targets:
                core.add_member(s, c, p, mode=mode, element_key=element_key, note=note)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"{c.key}: +{len(targets)} position(s) as {mode}")


@cascade.command("rm")
@click.argument("key")
@click.argument("pids", type=int, nargs=-1, required=True)
def cascade_rm(key, pids):
    """Take positions out of a cascade. The positions themselves are untouched."""
    with db.session() as s:
        try:
            c = core.cascade(s, key)
            targets = core.positions(s, pids)
            for p in targets:
                core.remove_member(s, c, p)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"{c.key}: -{len(targets)} position(s)")


@cascade.command("promote")
@click.argument("key")
@click.option("--to", "standing", type=click.Choice(CASCADE_STANDINGS), default="actual",
              show_default=True)
def cascade_promote(key, standing):
    """Make a candidate actual (or demote one).

    Promotion is what puts a cascade's box1/box2 into every session's Direction
    block, so it is a deliberate verb rather than a field.
    """
    with db.session() as s:
        try:
            c = core.cascade(s, key)
            was = core.set_standing(s, c, standing)
        except core.InvariantError as exc:
            raise click.ClickException(str(exc)) from exc
        s.commit()
        click.echo(f"{c.key}: {was} → {c.standing}"
                   + (" (now visible to prime and report)" if c.standing == "actual" else ""))


@cascade.command("shared")
@click.option("--element", help="one box only, e.g. ptw.box4")
@click.option("--include-candidates", is_flag=True,
              help="count candidate cascades too — inflates the platform with hypotheses")
@click.option("--verticals", is_flag=True, help="list the single-cascade rows in full")
def cascade_shared(element, include_candidates, verticals):
    """What more than one cascade rests on — the platform — and what only one does.

    Positions held by several cascades are the horizontal layer: load-bearing
    across brands, expensive to contest, and the thing that makes the portfolio
    one portfolio. Positions held by exactly one are that brand's vertical.
    """
    with db.session() as s:
        d = queries.shared_positions(
            s, include_candidates=include_candidates, element=element
        )
        n_casc = len(d["cascades"])
        if not n_casc:
            raise click.ClickException("no cascades to compare — see `strategy cascade create`")
        click.echo(
            f"{n_casc} cascade(s): " + " ".join(c.key for c in d["cascades"])
            + ("" if include_candidates else "  (candidates excluded)")
        )
        dist = d["distribution"]
        if dist:
            click.echo(
                "spread: "
                + " · ".join(f"{cnt} held by {n}" for n, cnt in dist.items())
            )
        click.echo()

        for n, _cnt in dist.items():
            group = [r for r in d["rows"] if r["n"] == n]
            if n == 1 and not verticals:
                per: dict[str, int] = {}
                for r in group:
                    per[r["cascades"][0]] = per.get(r["cascades"][0], 0) + 1
                click.echo(
                    f"held by 1 — the verticals ({len(group)}): "
                    + " · ".join(f"{k} {v}" for k, v in sorted(per.items()))
                    + "   (--verticals to list)"
                )
                continue
            # The platform is the top of the spread ACTUALLY observed, not
            # "held by every cascade": `hobby` holds one row, so a strict
            # all-cascades test would name nothing and the concept the report
            # exists to show would never appear.
            top = max((k for k in dist if k > 1), default=None)
            label = "the platform" if n == top else "shared"
            if n == 1:
                label = "the verticals"
            click.echo(f"held by {n} — {label} ({len(group)})")
            for r in group:
                p = r["position"]
                click.echo(
                    f"  {STATUS_MARK[p.status]} {(p.element_key or '—'):<9} "
                    f"#{p.id:<3} {' '.join(r['cascades']):<16} {p.title[:58]}"
                )
            click.echo()

        u = d["unassigned"]
        click.echo(
            f"in no cascade: {u['total']}"
            f"  ({u['retired']} retired, {u['no_box']} in no framework box"
            f", {u['candidate_only']} candidate-only"
            f", {len(u['missed'])} boxed and current)"
        )
        for p in u["missed"]:
            # The only bucket that means one was missed.
            click.echo(f"  MISSED {(p.element_key or '—'):<9} #{p.id:<3} {p.title[:58]}")
