"""FastAPI app for the admin surface. Read-only: there are no POST routes.

Every route is a rendering of something the CLI already computes — via
`queries` (positions, pending, gates, drift, store health) or
`report.build_context` (the cascade, findings, matrix). Nothing is queried here
that the CLI cannot also answer, which is the point: this surface exists to make
the CLI's capabilities visible, not to grow a second set of them.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import beadlink, core, db, queries
from ..frameworks import FRAMEWORKS
from ..models import (
    FACTOR_KINDS,
    SOURCE_SYSTEMS,
    STANCES,
    STATUSES,
    Drop,
    Metric,
    Position,
)
from . import commands as cmdmap
from .pages import (
    actions,
    case,
    cmd,
    days_cell,
    esc,
    fmt_date,
    pos_link,
    shell,
    status_cell,
    table,
    tile,
)


def _store_line() -> str:
    cfg = queries.store_config()
    stamp = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"
    return f"store {cfg['url']} · from {cfg['origin']} · rendered {stamp}"


def create_app() -> FastAPI:
    app = FastAPI(title="strategy — admin surface", docs_url=None, redoc_url=None)

    def get_db():
        s = db.session()
        try:
            yield s
        finally:
            s.close()

    def page(title: str, active: str, body: str) -> HTMLResponse:
        return HTMLResponse(shell(title, active, body, store_line=_store_line()))

    # ---------------------------------------------------------------- health
    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            with db.session() as s:
                s.scalar(select(Position.id).limit(1))
            return JSONResponse({"ok": True, "store": queries.store_config()["url"]})
        except Exception as exc:  # the store being unreachable IS the health answer
            return JSONResponse({"ok": False, "error": str(exc)[:300]}, status_code=503)

    # --------------------------------------------------------------- cascade
    @app.get("/", response_class=HTMLResponse)
    async def cascade(s: Session = Depends(get_db)):
        from ..report import build_context

        ctx = build_context("ptw")
        gates = queries.gates(s)
        pend = queries.pending(s)
        drifted = queries.drift(s)
        contested = [
            r for e in ctx["elements"] for r in e["rows"] if r["status"] == "contested"
        ]
        soon = [g for g in gates if g["days"] is not None and g["days"] <= 30]
        undated = [g for g in gates if g["undated"]]
        missed = [g for g in gates if g["missed"]]

        strip = "".join(
            [
                tile("positions", ctx["scope"]["current"], "/positions"),
                tile("contested", len(contested), "/positions?status=contested",
                     "hot" if contested else "good"),
                tile("pending", len(pend), "/pending", "hot" if pend else "good"),
                tile("gates ≤30d", len(soon), "/gates", "hot" if soon else ""),
                tile("undated gates", len(undated), "/gates", "hot" if undated else "good"),
                tile("missed", len(missed), "/gates", "hot" if missed else "good"),
                tile("drift", len(drifted), "/drift", "hot" if drifted else "good"),
                tile("findings", len(ctx["findings"]), "/findings"),
            ]
        )

        bluf = ""
        if ctx["bluf"]:
            items = "".join(
                f'<li><b>{esc(b["head"])}</b> {esc(b["body"])} '
                f'<span class="pill">{esc(b["ref"])}</span></li>'
                for b in ctx["bluf"]
            )
            bluf = f'<div class="warnbar"><b>Needs you</b><ul class="rows">{items}</ul></div>'

        boxes = ""
        for e in ctx["elements"]:
            rows = ""
            for r in e["rows"]:
                # The cascade is for reading the strategy at a glance, so it
                # carries the claim and nothing else. Status is a mark, not a
                # word, and only when it is NOT resolved — a decided position
                # saying "resolved" on every line is noise, while holding and
                # contested are the two states a reader has to notice. Scope,
                # date, source ref and sibling refs live on the detail page.
                mark = (
                    ""
                    if r["status"] == "resolved"
                    else f'<span class="st-{r["status"]}">{r["mark"]}</span> '
                )
                stmt = (r.get("statement_excerpt") or "").strip()
                body = (
                    f'<div class="claim">{esc(stmt)}</div>' if stmt else ""
                )
                rows += (
                    f'<li class="cascade-row">{mark}'
                    f'<b>{pos_link(r["id"], r["title"], 120)}</b>'
                    f'{body}</li>'
                )
            if not rows:
                rows = '<li class="st-contested">empty — nothing can be checked against it</li>'
            boxes += (
                f'<div class="box"><h3>{e["ordinal"]}. {esc(e["name"])} '
                f'<a class="pill" href="/positions?element={esc(e["key"])}">'
                f'{esc(e["key"])}</a></h3>'
                f'<p class="prompt">{esc(e["prompt"])}</p>'
                f'<ul class="rows">{rows}</ul></div>'
            )

        body = (
            f"<h1>{esc(ctx['fw']['name'])} — the cascade</h1>"
            + cmd(
                "strategy list · strategy report",
                "The admin view of the store: every current position in its framework box, "
                "with the raw source ref and declared scope on every row. Curated surfaces "
                "stage a subset of this; nothing is hidden here.",
            )
            + f'<div class="strip">{strip}</div>'
            + bluf
            + boxes
            + f'<p class="note">{esc(ctx["scope"]["by_scope_line"])} · '
            f'{ctx["scope"]["superseded"]} superseded · '
            f'{ctx["scope"]["evidence"]} evidence rows'
            + (f' · synced {esc(ctx["scope"]["synced_line"])}'
               if ctx["scope"]["synced_line"] else "")
            + "</p>"
        )
        return page("Cascade", "/", body)

    # ------------------------------------------------------------- positions
    @app.get("/positions", response_class=HTMLResponse)
    async def positions_list(
        s: Session = Depends(get_db),
        status: str | None = Query(None),
        element: str | None = Query(None),
        source: str | None = Query(None),
        venture: str | None = Query(None),
        all: bool = Query(False),
    ):
        rows, supers = queries.positions(
            s, status=status, element=element, source=source, venture=venture, show_all=all
        )
        ev = queries.evidence_counts(s)

        def bar(name: str, param: str, values: list[str], current: str | None) -> str:
            def link(label: str, val: str | None) -> str:
                q = {
                    "status": status, "element": element, "source": source,
                    "venture": venture, "all": "1" if all else None,
                }
                q[param] = val
                qs = "&".join(f"{k}={v}" for k, v in q.items() if v)
                on = "on" if (val or None) == (current or None) else ""
                href = "/positions" + ("?" + qs if qs else "")
                return f'<a class="{on}" href="{href}">{esc(label)}</a>'

            return (
                f'<div class="filters"><span>{esc(name)}</span>'
                + link("any", None)
                + "".join(link(v, v) for v in values)
                + "</div>"
            )

        elements = [k for k, *_ in FRAMEWORKS["ptw"]["elements"]] + ["none"]
        ventures = sorted({p.venture for p in s.scalars(select(Position)) if p.venture})
        filters = (
            bar("status", "status", list(STATUSES), status)
            + bar("box", "element", elements, element)
            + bar("source", "source", list(SOURCE_SYSTEMS), source)
            + bar("venture", "venture", ventures + ["none"], venture)
        )

        trs = []
        for p in rows:
            sup = " ⤴" if p.id in supers else ""
            trs.append(
                [
                    f'<a href="/positions/{p.id}">#{p.id}</a>',
                    status_cell(p.status),
                    f'<span class="m">{esc(p.element_key or "—")}</span>',
                    f'<span class="m">{esc(p.scope)}</span>',
                    f'<span class="m">{fmt_date(p.decided_on)}</span>',
                    pos_link(p.id, p.title, 100) + sup,
                    f'<span class="m">{esc(p.source_system)}:{esc(p.source_ref)}</span>',
                    str(ev.get(p.id, 0)),
                ]
            )
        flags = " ".join(
            f"--{k} {v}" for k, v in (
                ("status", status), ("element", element), ("source", source)
            ) if v
        )
        body = (
            "<h1>Positions</h1>"
            + cmd(
                f"strategy list {flags}".strip() + (" --all" if all else ""),
                "Superseded rows are hidden unless you ask for them; ⤴ marks a row that "
                "something else replaced.",
            )
            + filters
            + f'<div class="filters"><span>superseded</span>'
            f'<a class="{"" if all else "on"}" href="/positions">hidden</a>'
            f'<a class="{"on" if all else ""}" href="/positions?all=1">shown</a></div>'
            + table(
                ["id", "status", "box", "scope", "decided", "title", "source", "ev"],
                trs,
                numeric={7},
            )
            + f'<p class="note">{len(trs)} rows.</p>'
        )
        return page("Positions", "/positions", body)

    @app.get("/positions/{pid}", response_class=HTMLResponse)
    async def position_detail(pid: int, s: Session = Depends(get_db)):
        p = s.get(Position, pid)
        if not p:
            return page("Not found", "/positions", f"<h1>No position #{pid}</h1>")
        ev = sorted(p.evidence, key=lambda e: (e.observed_on or date.min, e.id))
        ev_rows = [
            [
                f'<span class="m">{fmt_date(e.observed_on)}</span>',
                f'<span class="m">{esc(e.scope)}</span>',
                f'<span class="m">{esc(e.source)}</span>',
                esc(e.summary),
            ]
            for e in ev
        ]
        chain = ""
        # supersedes is a LIST (a position may replace several) and superseded_by
        # is the single successor — the cardinality flipped on 2026-08-30 so that
        # consolidating a box into a handful is expressible.
        for q in p.supersedes:
            chain += f'<li>supersedes {pos_link(q.id, f"#{q.id} {q.title}")}</li>'
        if p.superseded_by:
            q = p.superseded_by
            chain += f'<li>superseded by {pos_link(q.id, f"#{q.id} {q.title}")}</li>'
        if p.claim_key:
            siblings = [
                q for q in s.scalars(select(Position).where(Position.claim_key == p.claim_key))
                if q.id != p.id
            ]
            for q in siblings:
                chain += (
                    f'<li>same question ({esc(p.claim_key)}) as '
                    f'{pos_link(q.id, f"#{q.id} {q.title}")} — {esc(q.status)}</li>'
                )
        gate_rows = [
            [
                f'<span class="m">{fmt_date(g.due_on)}</span>',
                esc(g.title),
                esc(g.gates[:160]),
            ]
            for g in (getattr(p, "forcings", []) or [])
        ]
        metric_rows = [
            [
                f'<a href="/metrics">{esc(m.name)}</a>',
                f'<span class="m">{esc(m.gates or "—")}</span>',
                f'<span class="m">{m.latest.value if m.latest else "—"}</span>',
                f'<span class="m">{fmt_date(m.latest.as_of) if m.latest else "—"}</span>',
            ]
            for m in (getattr(p, "metrics", []) or [])
        ]

        body = (
            f"<h1>#{p.id} {esc(p.title)}</h1>"
            + cmd(f"strategy show {p.id}")
            + '<div class="strip">'
            + tile("status", p.status)
            + tile("scope", p.scope)
            + tile("box", p.element_key or "—")
            + tile("decided", fmt_date(p.decided_on))
            + tile("venture", p.venture or "portfolio")
            + "</div>"
            + f'<p class="note">source <span class="pill">{esc(p.source_system)}:'
            f'{esc(p.source_ref)}</span>'
            + (f' source status <span class="pill">{esc(p.source_status)}</span>'
               if p.source_status else "")
            + (f' synced <span class="pill">{p.synced_at:%Y-%m-%d %H:%M}</span>'
               if p.synced_at else "")
            + "</p>"
            + (f'<h2>Statement</h2><pre class="doc">{esc(p.statement)}</pre>'
               if p.statement.strip() else "")
            + (f"<h2>Chain</h2><ul class='rows'>{chain}</ul>" if chain else "")
            + f"<h2>Evidence ({len(ev)})</h2>"
            + table(["observed", "scope", "source", "summary"], ev_rows)
            + (f"<h2>Gates</h2>{table(['due', 'gate', 'what it gates'], gate_rows)}"
               if gate_rows else "")
            + (f"<h2>Metrics</h2>{table(['metric', 'gates', 'latest', 'as of'], metric_rows)}"
               if metric_rows else "")
        )
        return page(f"#{p.id}", "/positions", body)

    # ----------------------------------------------------------------- triage
    #
    # The first WRITE routes on this surface. Every one of them calls `core` and
    # nothing else: the supersession scope rule, the store-owned-column boundary,
    # the required rejection reason and the all-or-nothing id check live there, and
    # a second surface must CALL them rather than re-implement them (position #67).
    # If a rule needs changing it changes in core and both surfaces get it.

    def _same_origin(request: Request) -> None:
        """Refuse a cross-site form POST.

        This surface binds to loopback and has no login, which is fine while it
        only reads. Once it writes, any page in the same browser can POST a form
        at 127.0.0.1 and the browser will send it. Browsers set Origin on form
        POSTs, so requiring it to match is enough to close that door without
        adding a session or a token to a single-user tool."""
        origin = request.headers.get("origin")
        if origin is None:
            return  # curl, tests, and same-origin GET-initiated navigations
        host = request.headers.get("host", "")
        if origin.split("://")[-1] != host:
            raise PermissionError(f"cross-origin write refused (origin {origin})")

    def _redirect(pid: int | None, flash: str = "", err: str = "") -> RedirectResponse:
        base = f"/triage/{pid}" if pid else "/triage"
        q = urlencode({k: v for k, v in (("flash", flash), ("err", err)) if v})
        return RedirectResponse(base + (f"?{q}" if q else ""), status_code=303)

    def _act(request: Request, s: Session, pid: int, fn) -> RedirectResponse:
        """Run one triage action: guard, call core, commit, land somewhere useful.

        A refusal is shown on the position it was refused on — never swallowed,
        and never a stack trace."""
        try:
            _same_origin(request)
        except PermissionError as exc:
            return _redirect(pid, err=str(exc))
        try:
            fn()
        except core.InvariantError as exc:
            s.rollback()
            return _redirect(pid, err=str(exc))
        s.commit()
        return None

    def _next_after(s: Session, pid: int) -> int | None:
        q = [p.id for p in queries.triage_queue(s)]
        if pid in q:
            i = q.index(pid)
            return q[i + 1] if i + 1 < len(q) else None
        return q[0] if q else None

    @app.get("/triage", response_class=HTMLResponse)
    async def triage_first(s: Session = Depends(get_db)):
        q = queries.open_issues(s)
        if not q:
            return page(
                "Triage",
                "/triage",
                "<h1>Nothing open</h1>"
                + cmd("strategy triage",
                      "No undated drafts, no live disputes, no open questions.")
                + '<p class="note">Every position carries a decided date and nobody is '
                "waiting on an answer.</p>",
            )
        return RedirectResponse(f"/triage/{q[0]['position'].id}", status_code=303)

    @app.get("/triage/{pid}", response_class=HTMLResponse)
    async def triage_card(pid: int, s: Session = Depends(get_db),
                          flash: str = Query(""), err: str = Query("")):
        d = queries.dossier(s, pid)
        if not d:
            return page("Triage", "/triage", f"<h1>No position #{pid}</h1>")
        p = d["position"]
        # One bulk read for the whole queue — the strip used to call dossier per
        # entry, which cost the page 27 seconds against a hosted store.
        q = queries.open_issues(s)
        ids = [r["position"].id for r in q]
        pos = ids.index(pid) + 1 if pid in ids else None
        strip = "".join(
            f'<a class="{"on" if r["position"].id == pid else ""}'
            f'{" bare" if r["bare"] else ""}" '
            f'href="/triage/{r["position"].id}" '
            f'title="{esc(r["position"].title[:80])}">#{r["position"].id}</a>'
            for r in q
        )
        gates = "".join(
            f'<div class="warnbar"><b>GATE {fmt_date(g.due_on)}</b> {esc(g.title)} — '
            f"{esc(g.gates[:200])}</div>"
            for g in d["gates"]
        )
        head = ""
        if flash:
            head += f'<div class="flash">{esc(flash)}</div>'
        if err:
            head += f'<div class="flash err"><b>Refused.</b> {esc(err)}</div>'
        body = (
            head
            + f'<h1>#{p.id} {esc(p.title)}</h1>'
            + cmd(
                f"strategy triage --id {p.id}",
                "Decide it, or say what you would need to know. Every action here goes "
                "through the same core the CLI calls, so the two cannot disagree about "
                "what the store allows.",
            )
            + (f'<p class="note">{pos} of {len(q)} open</p>' if pos else
               '<p class="note">not currently an open issue — shown because you asked for it</p>')
            + f'<div class="queue">{strip}</div>'
            + gates
            + '<div class="strip">'
            + tile("status", p.status)
            + tile("scope", p.scope)
            + tile("box", p.element_key or "—")
            + tile("decided", fmt_date(p.decided_on))
            + tile("factors", d["factors_n"])
            + tile("evidence", d["evidence_n"])
            + "</div>"
            + (f'<details open><summary class="m">statement</summary>'
               f'<pre class="doc">{esc(p.statement)}</pre></details>'
               if p.statement.strip() else "")
            + case(d)
            + actions(p.id, STANCES, FACTOR_KINDS)
            + f'<p class="note">Source <span class="pill">{esc(p.source_system)}:'
            f'{esc(p.source_ref)}</span> · '
            f'<a href="/positions/{p.id}">full position page</a></p>'
        )
        return page(f"Triage #{p.id}", "/triage", body)

    @app.post("/triage/{pid}/adopt")
    async def triage_adopt(pid: int, request: Request, s: Session = Depends(get_db),
                           note: str = Form("")):
        nxt = _next_after(s, pid)
        bad = _act(request, s, pid, lambda: core.adopt(s, [pid], note=note or None))
        return bad or _redirect(nxt, flash=f"#{pid} adopted.")

    @app.post("/triage/{pid}/reject")
    async def triage_reject(pid: int, request: Request, s: Session = Depends(get_db),
                            note: str = Form("")):
        nxt = _next_after(s, pid)
        bad = _act(request, s, pid, lambda: core.reject(s, [pid], note=note or None))
        return bad or _redirect(nxt, flash=f"#{pid} rejected.")

    @app.post("/triage/{pid}/ask")
    async def triage_ask(pid: int, request: Request, s: Session = Depends(get_db),
                         text: str = Form(...)):
        bad = _act(request, s, pid, lambda: core.ask(s, pid, text=text))
        return bad or _redirect(pid, flash="Flagged — it stays open, now blocked on an answer.")

    @app.post("/triage/{pid}/factor")
    async def triage_factor(pid: int, request: Request, s: Session = Depends(get_db),
                            kind: str = Form(...), text: str = Form(...)):
        bad = _act(request, s, pid,
                   lambda: core.add_factor(s, pid, kind=kind, text=text, source="triage"))
        return bad or _redirect(pid, flash="Factor added.")

    @app.post("/triage/{pid}/note")
    async def triage_note(pid: int, request: Request, s: Session = Depends(get_db),
                          text: str = Form(...), stance: str = Form("")):
        p = s.get(Position, pid)
        bad = _act(
            request, s, pid,
            lambda: core.add_evidence(s, pid, source="triage note", summary=text,
                                      scope=p.scope if p else "single-rig",
                                      observed_on=date.today(), stance=stance or None),
        )
        return bad or _redirect(pid, flash="Observation recorded.")

    @app.post("/triage/{pid}/skip")
    async def triage_skip(pid: int, request: Request, s: Session = Depends(get_db)):
        return _redirect(_next_after(s, pid))

    # ---------------------------------------------------------------- pending
    @app.get("/pending", response_class=HTMLResponse)
    async def pending_page(s: Session = Depends(get_db)):
        rows = queries.pending(s)
        trs = [
            [
                f'<a href="/positions/{p.id}">#{p.id}</a>',
                status_cell(p.status),
                f'<span class="m">{esc(p.element_key or "—")}</span>',
                f'<span class="m">{esc(p.scope)}</span>',
                pos_link(p.id, p.title, 110),
                f'<span class="m">{esc(p.source_system)}:{esc(p.source_ref)}</span>',
            ]
            for p in rows
        ]
        body = (
            "<h1>Pending — drafted, never adopted</h1>"
            + cmd(
                "strategy pending  ·  strategy adopt <id>  ·  strategy return <id>",
                "'Drafted' is decided_on IS NULL: a claim nobody has dated is a claim nobody "
                "has decided. Adopting is a write, so it stays on the CLI.",
            )
            + table(["id", "status", "box", "scope", "title", "source"], trs)
            + f'<p class="note">{len(trs)} awaiting adoption.</p>'
        )
        return page("Pending", "/pending", body)

    def satisfied_cell(g) -> str:
        """What would close this gate — the column that makes a gate checkable.

        A BEAD can be read from its rig; an EVENT cannot, ever, and has to be
        someone's job on the day. Rendering them the same way would imply the
        second kind gets watched by the same machinery as the first."""
        sats = g["satisfiers"]
        if not sats:
            return ('<span class="m">— <span title="nobody has looked yet; not a '
                    'claim that nothing could satisfy it">unclassified</span></span>')
        parts = []
        for sat in sats:
            if sat.kind == "event":
                parts.append(f'<span class="pill">event</span> {esc(sat.note[:70])}')
            elif sat.kind == "position":
                parts.append(
                    f'<span class="pill">position</span> '
                    f'<a href="/positions/{sat.position_id}">#{sat.position_id}</a> '
                    f'{esc(sat.predicate or "")}'
                )
            else:
                parts.append(f'<span class="pill">bead</span> <code>{esc(sat.bead_id)}</code>')
        return "<br>".join(parts)

    def unclassified_bar(rows) -> str:
        """Say how much of the table nothing is watching, on the page itself.

        A drift report you have to remember to run reproduces the failure it was
        built to fix (docs/two-writers-one-row.md)."""
        n = sum(1 for g in rows if g["satisfaction"] == beadlink.UNCLASSIFIED)
        if not n:
            return ""
        return (
            f'<div class="warnbar"><b>{n} of {len(rows)} gates name nothing that would '
            f"satisfy them</b> — nobody has recorded whether they close on work "
            f"(<code>strategy gate link #N bead-id</code>) or on a day "
            f"(<code>strategy gate link #N --event \"…\"</code>). Only the first kind "
            f"can be checked without a person.</div>"
        )

    # ------------------------------------------------------------------ gates
    @app.get("/gates", response_class=HTMLResponse)
    async def gates_page(s: Session = Depends(get_db), all: bool = Query(False)):
        rows = queries.gates(s, show_all=all)
        trs = [
            [
                f'<span class="m">{fmt_date(g["row"].due_on)}</span>',
                days_cell(g["days"], g["missed"]),
                f'<span class="m">{esc(g["row"].venture or "portfolio")}</span>',
                esc(g["row"].title)
                + (f' <a class="pill" href="/positions/{g["row"].position_id}">'
                   f'#{g["row"].position_id}</a>' if g["row"].position_id else ""),
                esc(g["row"].condition or "—"),
                esc(g["row"].gates or "—"),
                satisfied_cell(g),
            ]
            for g in rows
        ]
        undated = sum(1 for g in rows if g["undated"])
        missed = sum(1 for g in rows if g["missed"])
        warn = ""
        if undated or missed:
            warn = (
                f'<div class="warnbar"><b>{undated} gates carry no date</b> — a deferral '
                f"without a trigger is itself the finding; give each a day it can be true "
                f"on, or drop it."
                + (f" <b>{missed} missed.</b>" if missed else "")
                + "</div>"
            )
        body = (
            "<h1>Forcing gates</h1>"
            + cmd(
                "strategy forcing" + (" --all" if all else ""),
                "Soonest first; undated last — they are the finding, not a tail. "
                "'What it gates' is the column that turns a list of dates into a graph.",
            )
            + '<div class="filters"><span>resolved</span>'
            f'<a class="{"" if all else "on"}" href="/gates">hidden</a>'
            f'<a class="{"on" if all else ""}" href="/gates?all=1">shown</a></div>'
            + warn
            + unclassified_bar(rows)
            + table(
                ["due", "in", "venture", "gate", "condition", "what it gates", "satisfied by"],
                trs,
            )
        )
        return page("Gates", "/gates", body)

    # ------------------------------------------------------------------ drift
    @app.get("/drift", response_class=HTMLResponse)
    async def drift_page(s: Session = Depends(get_db)):
        rows = queries.drift(s)
        trs = [
            [
                f'<a href="/positions/{d["position"].id}">#{d["position"].id}</a>',
                f'<span class="m">{esc(d["position"].source_system)}:'
                f'{esc(d["position"].source_ref)}</span>',
                status_cell(d["store"]),
                f'<span class="m">{esc(d["source_maps_to"])}</span>',
                pos_link(d["position"].id, d["position"].title, 90),
            ]
            for d in rows
        ]
        body = (
            "<h1>Drift — source vs store</h1>"
            + cmd(
                "strategy reconcile",
                "Reports only. The store is authoritative for status by design, so this is a "
                "list to read, never a list to apply: a sync must never rewrite judgment.",
            )
            + table(["id", "source", "store says", "source maps to", "title"], trs)
            + f'<p class="note">{len(trs)} drifted. Resolve with '
            "<code>strategy set &lt;id&gt; --status …</code>.</p>"
        )
        return page("Drift", "/drift", body)

    # --------------------------------------------------------------- findings
    @app.get("/findings", response_class=HTMLResponse)
    async def findings_page():
        from ..report import build_context

        ctx = build_context("ptw")
        out = ""
        for f in ctx["findings"]:
            items = "".join(f"<li>{esc(r)}</li>" for r in f["rows"])
            out += (
                f'<div class="box"><h3>{esc(f["title"])}</h3>'
                f'<p class="prompt">{esc(f["why"])}</p>'
                f'<ul class="rows">{items}</ul></div>'
            )
        body = (
            "<h1>Findings — what the store can see about itself</h1>"
            + cmd(
                "strategy report --detail",
                "Structural findings only: orphaned positions, empty required boxes, "
                "undated gates, claims answered two ways. Not opinions about the strategy.",
            )
            + (out or '<p class="note">no findings</p>')
        )
        return page("Findings", "/findings", body)

    # ----------------------------------------------------------------- matrix
    @app.get("/matrix", response_class=HTMLResponse)
    async def matrix_page():
        from ..report import build_context

        ctx = build_context("ptw")
        m = ctx["matrix"]
        headers = ["test"] + m["ventures"]
        trs = []
        for r in m["rows"]:
            cells = [f'<span class="m" title="{esc(r["text"])}">{esc(r["key"])}</span> '
                     f'{esc(r["text"])}']
            for c in r["cells"]:
                mark = c["mark"] or "·"
                cls = {"pass": "st-resolved", "fail": "st-contested"}.get(
                    c["verdict"], "st-holding"
                )
                cells.append(f'<span class="{cls}" title="{esc(c["note"])}">{mark}</span>')
            trs.append(cells)
        notes = "".join(
            f'<li><span class="pill">{esc(n["test"])} · {esc(n["venture"])} · '
            f'{esc(n["verdict"])}</span> {esc(n["note"])}</li>'
            for n in m["notes"]
        )
        body = (
            "<h1>Tests × ventures</h1>"
            + cmd(
                "strategy assess [venture] [test]",
                "An unanswered cell stays blank — never defaulted to a pass, which would "
                "invent an opinion nobody holds. · = nobody has answered it.",
            )
            + f'<p class="note">{m["answered"]} of {m["total"]} cells answered.</p>'
            + table(headers, trs, numeric=set(range(1, len(headers))))
            + (f"<h2>Notes</h2><ul class='rows'>{notes}</ul>" if notes else "")
        )
        return page("Matrix", "/matrix", body)

    # ---------------------------------------------------------------- metrics
    @app.get("/metrics", response_class=HTMLResponse)
    async def metrics_page(s: Session = Depends(get_db)):
        rows = sorted(s.scalars(select(Metric)), key=lambda m: (m.beat, m.name))
        trs = []
        for m in rows:
            latest = m.latest
            prior = None
            if latest:
                earlier = [r for r in m.readings if r.as_of < latest.as_of]
                prior = earlier[-1] if earlier else None
            delta = (
                f"{latest.value - prior.value:+d} since {prior.as_of}"
                if (latest and prior)
                else "—"
            )
            nxt = m.next_gate()
            trs.append(
                [
                    esc(m.name),
                    f'<span class="m">{esc(m.beat)}</span>',
                    f'<span class="m">{esc(m.source)}</span>',
                    str(latest.value) if latest else "—",
                    f'<span class="m">{fmt_date(latest.as_of) if latest else "—"}</span>',
                    f'<span class="m">{esc(delta)}</span>',
                    f'<span class="m">{esc(m.gates or "—")}</span>',
                    f'<span class="m">{nxt if nxt else "—"}</span>',
                    f'<span class="m">{fmt_date(m.due_on)}</span>',
                    (f'<a href="/positions/{m.position_id}">#{m.position_id}</a>'
                     if m.position_id else "—"),
                    str(len(m.readings)),
                ]
            )
        body = (
            "<h1>Metrics</h1>"
            + cmd(
                "strategy metric list · strategy metric read <name> <value>",
                "Readings are a time series, never an overwrite. 'next' is the next gate the "
                "latest reading has not cleared.",
            )
            + table(
                ["metric", "beat", "source", "latest", "as of", "delta", "gates",
                 "next", "due", "position", "n"],
                trs,
                numeric={3, 7, 10},
            )
        )
        return page("Metrics", "/metrics", body)

    # --------------------------------------------------------------- cascades
    @app.get("/cascades", response_class=HTMLResponse)
    async def cascades_page(s: Session = Depends(get_db)):
        """Every cascade. Actuals first, because candidates are not direction."""
        from ..models import Cascade, CascadePosition

        rows = [c for c in s.scalars(select(Cascade)) if c.retired_on is None]
        counts = dict(
            s.execute(
                select(CascadePosition.cascade_id, func.count()).group_by(
                    CascadePosition.cascade_id
                )
            ).all()
        )
        by_id = {c.id: c for c in s.scalars(select(Cascade))}
        rows.sort(key=lambda c: (c.standing != "actual", c.brand or "~", c.key))
        trs = []
        for c in rows:
            parent = by_id.get(c.forked_from_id)
            trs.append(
                [
                    f'<a href="/cascades/{esc(c.key)}">{esc(c.key)}</a>',
                    esc(c.brand or "—"),
                    (
                        f'<span class="m">{esc(c.standing)}</span>'
                        if c.standing == "actual"
                        else f'<span class="m">{esc(c.standing)} — not in prime</span>'
                    ),
                    f'<span class="m">{esc(c.direction)}</span>',
                    str(counts.get(c.id, 0)),
                    (
                        f'<a href="/cascades/{esc(parent.key)}">{esc(parent.key)}</a>'
                        if parent
                        else "—"
                    ),
                    f'<span class="m">{esc(c.question or "—")}</span>',
                ]
            )
        # The layer/vertical report, on the same page: the counts above only mean
        # something next to what the cascades SHARE (solutions-tx3.5).
        d = queries.shared_positions(s)
        dist = d["distribution"]
        top = max((k for k in dist if k > 1), default=None)
        shared_parts = []
        for n, _cnt in dist.items():
            group = [r for r in d["rows"] if r["n"] == n]
            label = "the platform" if n == top else ("the verticals" if n == 1 else "shared")
            shared_parts.append(f"<h3>held by {n} — {esc(label)} ({len(group)})</h3>")
            if n == 1:
                per: dict[str, int] = {}
                for r in group:
                    per[r["cascades"][0]] = per.get(r["cascades"][0], 0) + 1
                shared_parts.append(
                    '<p class="m">'
                    + " · ".join(f"{esc(k)} {v}" for k, v in sorted(per.items()))
                    + "</p>"
                )
                continue
            shared_parts.append(
                table(
                    ["box", "position", "status", "held by"],
                    [
                        [
                            f'<span class="m">{esc(r["position"].element_key or "—")}</span>',
                            pos_link(r["position"].id, r["position"].title),
                            status_cell(r["position"].status),
                            f'<span class="m">{esc(" ".join(r["cascades"]))}</span>',
                        ]
                        for r in group
                    ],
                )
            )
        u = d["unassigned"]
        gap = (
            f'<p class="m">in no cascade: {u["total"]} — {u["retired"]} retired, '
            f'{u["no_box"]} in no framework box, {u["candidate_only"]} candidate-only, '
            f'{len(u["missed"])} boxed and current'
            + (
                " ("
                + " ".join(f'#{p.id}' for p in u["missed"])
                + ")"
                if u["missed"]
                else ""
            )
            + "</p>"
        )
        body = (
            "<h1>Cascades</h1>"
            + cmd(
                "strategy cascade list · strategy cascade show &lt;key&gt; · strategy cascade shared",
                "Cascades share positions rather than copying them. A candidate is a point "
                "of view being tried on — it is deliberately absent from prime and report.",
            )
            + table(
                ["cascade", "brand", "standing", "direction", "positions", "forked from", "asks"],
                trs,
                numeric={4},
            )
            + "<h2>What the cascades share</h2>"
            + cmd(
                "strategy cascade shared",
                "Positions several cascades rest on are the horizontal platform; positions "
                "exactly one holds are that brand's vertical. Candidates are excluded.",
            )
            + "".join(shared_parts)
            + gap
        )
        return page("Cascades", "/cascades", body)

    @app.get("/cascades/{key}", response_class=HTMLResponse)
    async def cascade_page(key: str, s: Session = Depends(get_db)):
        """One cascade as five boxes. An empty box is rendered, not dropped."""
        try:
            c = core.cascade(s, key)
        except core.InvariantError as exc:
            return page("Cascade", "/cascades", f"<h1>Cascades</h1><p>{esc(str(exc))}</p>")
        boxes = core.cascade_boxes(s, c)
        head = f"<h1>{esc(c.name)} <span class=\"m\">({esc(c.key)})</span></h1>"
        meta = " · ".join(
            x
            for x in (
                esc(c.standing),
                esc(c.direction),
                f"brand {esc(c.brand)}" if c.brand else "",
            )
            if x
        )
        parts = [head, cmd(f"strategy cascade show {esc(c.key)}", meta)]
        if c.question:
            parts.append(f"<p><b>Asks:</b> {esc(c.question)}</p>")
        if c.standing != "actual":
            parts.append(
                '<p class="m">Candidate — these rows are hypotheses and are '
                "excluded from prime and report.</p>"
            )
        for ekey, _ord, ename, _req, prompt in FRAMEWORKS[c.framework]["elements"]:
            members = boxes.get(ekey, [])
            parts.append(f"<h2>{esc(ekey)} — {esc(ename)}</h2>")
            if not members:
                parts.append(f'<p class="m">empty — {esc(prompt)}</p>')
                continue
            parts.append(
                table(
                    ["mode", "position", "status", "note"],
                    [
                        [
                            f'<span class="m">{esc(r["membership"].mode)}</span>',
                            pos_link(r["position"].id, r["position"].title),
                            status_cell(r["position"].status),
                            f'<span class="m">{esc(r["membership"].note or "—")}</span>',
                        ]
                        for r in members
                    ],
                )
            )
        return page(f"Cascade {c.key}", "/cascades", "".join(parts))

    # ------------------------------------------------------------------ drops
    @app.get("/drops", response_class=HTMLResponse)
    async def drops_page(s: Session = Depends(get_db), days: int = Query(60)):
        cutoff = date.today().fromordinal(date.today().toordinal() - days)
        rows = sorted(
            (d for d in s.scalars(select(Drop)) if d.posted_on >= cutoff),
            key=lambda d: (d.posted_on, d.id),
            reverse=True,
        )

        def call(d: Drop) -> str:
            if d.base_hit is None:
                return '<span class="st-contested">no stats</span>'
            return ('<span class="st-resolved">ON BASE</span>' if d.base_hit
                    else '<span class="st-holding">out</span>')

        trs = [
            [
                f'<span class="m">#{d.id}</span>',
                f'<span class="m">{fmt_date(d.posted_on)}</span>',
                f'<span class="m">{esc(d.format)}'
                + (f'·{esc(d.link_placement)}' if d.link_placement else "")
                + "</span>",
                f'<a href="{esc(d.url)}" rel="noopener noreferrer" target="_blank">'
                f'{esc((d.title or d.url)[:70])}</a>',
                str(d.impressions) if d.impressions is not None else "—",
                str(d.reached) if d.reached is not None else "—",
                str(d.others_comments) if d.others_comments is not None else "—",
                str(d.link_clicks) if d.link_clicks is not None else "—",
                str(d.followers_gained) if d.followers_gained is not None else "—",
                call(d),
                f'<span class="m">{fmt_date(d.stats_as_of)}</span>',
            ]
            for d in rows
        ]
        scored = [d for d in rows if d.has_stats]
        on_base = [d for d in scored if d.base_hit]
        body = (
            "<h1>Drops</h1>"
            + cmd(
                f"strategy drop list --days {days}",
                "Base hit = one follower gained, or a comment or repost that is not mine, or "
                "≥5 link clicks. A drop with no stats has not been scored, not failed.",
            )
            + '<div class="strip">'
            + tile("dropped", len(rows))
            + tile("scored", len(scored), kind="" if len(scored) == len(rows) else "hot")
            + tile("on base", len(on_base), kind="good" if on_base else "hot")
            + tile("unscored", len(rows) - len(scored),
                   kind="hot" if len(rows) != len(scored) else "good")
            + "</div>"
            + '<div class="filters"><span>window</span>'
            + "".join(
                f'<a class="{"on" if days == d else ""}" href="/drops?days={d}">{d}d</a>'
                for d in (7, 30, 60, 365)
            )
            + "</div>"
            + table(
                ["id", "posted", "medium", "title", "imp", "reach", "cmt", "clicks",
                 "follows", "call", "stats as of"],
                trs,
                numeric={4, 5, 6, 7, 8},
            )
        )
        return page("Drops", "/drops", body)

    # -------------------------------------------------------------- questions
    @app.get("/questions", response_class=HTMLResponse)
    async def questions_page(s: Session = Depends(get_db), all: bool = Query(False)):
        from .. import core

        rows = core.open_questions(s)
        if all:
            from ..models import Question

            rows = sorted(s.scalars(select(Question)), key=lambda q: (q.position_id, q.id))
        trs = []
        for q in rows:
            p = s.get(Position, q.position_id)
            trs.append(
                [
                    f'<span class="m">q{q.id}</span>',
                    f'<a href="/positions/{q.position_id}">#{q.position_id}</a>',
                    pos_link(q.position_id, p.title if p else "(missing)", 60),
                    f'<span class="m">{fmt_date(q.asked_on)}</span>',
                    esc(q.text),
                    (f'<span class="st-resolved">{fmt_date(q.answered_on)}</span><br>'
                     f'<span class="m">{esc(q.answer)}</span>')
                    if q.answered_on
                    else '<span class="st-contested">open</span>',
                ]
            )
        body = (
            "<h1>Open questions</h1>"
            + cmd(
                "strategy questions  ·  strategy ask <id> \"…\"  ·  strategy answer <qid> \"…\"",
                "The worklist triage produces. A pending position with an open question is "
                "BLOCKED — it has an owner and a next action; a pending position without one "
                "is merely untouched. The difference is the point of the table.",
            )
            + '<div class="filters"><span>show</span>'
            f'<a class="{"" if all else "on"}" href="/questions">open</a>'
            f'<a class="{"on" if all else ""}" href="/questions?all=1">all</a></div>'
            + table(["q", "pos", "position", "asked", "question", "answer"], trs)
            + f'<p class="note">{len(trs)} question(s).</p>'
        )
        return page("Questions", "/questions", body)

    # ----------------------------------------------------------------- funnel
    @app.get("/funnel", response_class=HTMLResponse)
    async def funnel_page(s: Session = Depends(get_db), days: int = Query(30)):
        from ..funnel import analytics_url, funnel_for

        head = (
            "<h1>Funnel — click to in-app event</h1>"
            + cmd(
                f"strategy drop funnel --days {days}",
                "LinkedIn clicks → landing views → what people actually did in the app, read "
                "from the page_views plane. An event is charged to a drop only when the row "
                "names the page the drop points at; anything else stays site-level. "
                "One query per drop against a SECOND database — the widest windows are slow.",
            )
        )
        if not analytics_url():
            return page(
                "Funnel",
                "/funnel",
                head
                + '<div class="warnbar"><b>No analytics database configured.</b> Set '
                "STRATEGY_ANALYTICS_URL, PAGEVIEWS_DB_URL or QRCARDS_RUNTIME_DB_URL "
                "(or put one in ~/.env). Without it this page cannot run — which is "
                "reported rather than rendered as zeroes.</div>",
            )
        cutoff = date.today().fromordinal(date.today().toordinal() - days)
        drops = sorted(
            (d for d in s.scalars(select(Drop)) if d.app_url and d.posted_on >= cutoff),
            key=lambda d: (d.posted_on, d.id),
            reverse=True,
        )
        trs = []
        for d in drops:
            f = funnel_for(d.app_url, d.posted_on)
            if not f:
                continue
            h = f["hib"]
            if any(h[k] for k in ("start", "era2", "era3", "complete")):
                tail = (f'opened {h["start"]} → era2 {h["era2"]} → era3 {h["era3"]} '
                        f'→ full trip {h["complete"]}')
            else:
                tail = (f'tables {f["tables_created"]} → joined {f["joined"]} → '
                        f'dealt {f["dealt"]} → completed {f["completed"]}')
            trs.append(
                [
                    f'<span class="m">#{d.id}</span>',
                    f'<span class="m">{fmt_date(d.posted_on)}</span>',
                    f'<a href="{esc(d.url)}" rel="noopener noreferrer" target="_blank">'
                    f'{esc((d.title or d.url)[:52])}</a>',
                    f'<span class="m">{esc(f["domain"])}</span>',
                    "—" if d.link_clicks is None else str(d.link_clicks),
                    str(f["landing_views"]),
                    str(f["landing_from_linkedin"]),
                    f'<span class="m">{esc(tail)}</span>',
                ]
            )
        body = (
            head
            + '<div class="filters"><span>window</span>'
            + "".join(
                f'<a class="{"on" if days == n else ""}" href="/funnel?days={n}">{n}d</a>'
                for n in (7, 30, 60, 365)
            )
            + "</div>"
            + table(
                ["id", "posted", "drop", "domain", "clicks", "landing", "from li", "in-app"],
                trs,
                numeric={4, 5, 6},
            )
            + '<p class="note">Drops with no app URL are absent: there is no funnel to read.</p>'
        )
        return page("Funnel", "/funnel", body)

    # ---------------------------------------------------------------- profile
    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(s: Session = Depends(get_db), surface: str = Query("linkedin")):
        from .. import profile as profile_mod

        findings = profile_mod.check(s, surface)
        fields = profile_mod._fields(s, surface)
        out = ""
        for f in findings:
            ran = f.get("ran", True)
            items = "".join(f"<li>{esc(r)}</li>" for r in f["rows"])
            head = esc(f["check"]) + ("" if ran else " — could not run")
            cls = "warnbar" if (f["rows"] or not ran) else "box"
            out += (
                f'<div class="{cls}"><h3>{head}</h3>'
                f'<p class="prompt">{esc(f["why"])}</p>'
                + (f'<ul class="rows">{items}</ul>' if items else
                   '<p class="note st-resolved">clear</p>')
                + "</div>"
            )
        field_rows = []
        for f in fields:
            claims = "".join(
                f'<li>{esc(c.claim_text)} <span class="pill">'
                f'{esc(c.licensed_by_kind)}'
                + (f":{esc(c.licensed_by_id)}" if c.licensed_by_id else "")
                + "</span></li>"
                for c in f.claims
            )
            over = f.char_limit and len(f.text) > f.char_limit
            field_rows.append(
                [
                    esc(f.field),
                    f'<span class="m{" st-contested" if over else ""}">'
                    f'{len(f.text)}{"/" + str(f.char_limit) if f.char_limit else ""}</span>',
                    f"<pre>{esc(f.text)}</pre>"
                    + (f'<ul class="rows">{claims}</ul>' if claims else ""),
                ]
            )
        body = (
            f"<h1>Public surface — {esc(surface)}</h1>"
            + cmd(
                f"strategy profile check --surface {surface}  ·  strategy profile export",
                "What the surface currently says, and what in the store licenses each factual "
                "claim. A claim with no licence is not an error to prevent — it is the finding.",
            )
            + out
            + "<h2>Fields</h2>"
            + table(["field", "chars", "text / claims"], field_rows)
        )
        return page("Profile", "/profile", body)

    # ----------------------------------------------------------------- report
    @app.get("/report", response_class=HTMLResponse)
    async def report_page(detail: bool = Query(False)):
        from ..report import render

        md = render("ptw", detail=detail)
        body = (
            "<h1>Rendered memo</h1>"
            + cmd(
                "strategy report" + (" --detail" if detail else ""),
                "The generated Markdown, exactly as `--write` would put it on disk. A printout, "
                "not the source.",
            )
            + '<div class="filters"><span>document</span>'
            f'<a class="{"" if detail else "on"}" href="/report">summary</a>'
            f'<a class="{"on" if detail else ""}" href="/report?detail=1">detail</a></div>'
            + f'<pre class="doc">{esc(md)}</pre>'
        )
        return page("Report", "/report", body)

    # ------------------------------------------------------------------ store
    @app.get("/store", response_class=HTMLResponse)
    async def store_page(s: Session = Depends(get_db)):
        cfg = queries.store_config()
        health = queries.store_health(s)
        settings = table(
            ["setting", "origin", "value"],
            [
                [f'<span class="m">{esc(k)}</span>',
                 f'<span class="m">{esc(o)}</span>',
                 f'<span class="m">{esc(v[:90])}</span>']
                for k, o, v in cfg["settings"]
            ],
        )
        src_rows = []
        for row in health["sources"]:
            if row["hours"] is None:
                age = '<span class="m">hand-entered, no upstream</span>'
            else:
                cls = "st-contested" if row["stale"] else "m"
                age = f'<span class="{cls}">{row["hours"]:.1f}h ago' \
                      f'{" — STALE" if row["stale"] else ""}</span>'
            src_rows.append([f'<span class="m">{esc(row["source"])}</span>',
                             str(row["rows"]), age])
        envs = "".join(
            f'<li><span class="m">{esc(p)}</span> '
            + ("" if ok else '<span class="st-contested">(absent)</span>')
            + "</li>"
            for p, ok in cfg["env_files"]
        )
        body = (
            "<h1>Store</h1>"
            + cmd(
                "strategy doctor",
                "Which store am I on, where did each setting come from, how stale is it. Two "
                "resolution paths for one setting is what made a bare shell and a sourced "
                "shell two different programs.",
            )
            + '<div class="strip">'
            + tile("positions", health["positions"], "/positions")
            + tile("evidence", health["evidence"])
            + tile("gates", health["gates"], "/gates")
            + tile("assessments", health["assessments"], "/matrix")
            + tile("drops", health["drops"], "/drops")
            + tile("metrics", health["metrics"], "/metrics")
            + "</div>"
            + f'<p class="note">store <span class="pill">{esc(cfg["url"])}</span> '
            f'from <span class="pill">{esc(cfg["origin"])}</span></p>'
            + f'<h2>.env search path</h2><ul class="rows">{envs}</ul>'
            + "<h2>Settings</h2>" + settings
            + "<h2>Sources</h2>" + table(["source", "rows", "synced"], src_rows, numeric={1})
            + ('<div class="warnbar"><b>Sources disagree by more than 12h</b> — a reseed is '
               "refreshing only some of them.</div>" if health["split"] else "")
        )
        return page("Store", "/store", body)

    # --------------------------------------------------------------- commands
    @app.get("/commands", response_class=HTMLResponse)
    async def commands_page():
        trs = []
        for c in cmdmap.COMMANDS:
            if c.surface:
                where = f'<a href="{esc(c.surface)}">{esc(c.surface)}</a>'
            elif c.writes:
                where = '<span class="m">CLI only — writes</span>'
            else:
                where = '<span class="st-contested">not surfaced</span>'
            trs.append(
                [
                    f'<span class="m">{esc(c.name)}</span>',
                    '<span class="st-contested">write</span>' if c.writes
                    else '<span class="st-resolved">read</span>',
                    where,
                    esc(c.what),
                ]
            )
        read = [c for c in cmdmap.COMMANDS if not c.writes]
        covered = [c for c in read if c.surface]
        body = (
            "<h1>Command coverage</h1>"
            + cmd(
                "strategy --help",
                "Every CLI command, and whether this surface shows it. Writes stay on the CLI "
                "by design (position #67): the invariants that protect judgment live in the "
                "command handlers, and a second writer must call them, not re-implement them.",
            )
            + '<div class="strip">'
            + tile("read commands", len(read))
            + tile("surfaced", len(covered),
                   kind="good" if len(covered) == len(read) else "hot")
            + tile("write commands", len([c for c in cmdmap.COMMANDS if c.writes]))
            + "</div>"
            + table(["command", "kind", "surfaced at", "what it does"], trs)
        )
        return page("Commands", "/commands", body)

    return app


app = None  # built by `strategy web`; kept out of import time so a bad store fails loudly there
