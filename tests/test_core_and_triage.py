"""The write core, and the triage loop over it.

Position #67 asked for the invariants to live in code both surfaces call. These
tests exercise them through `core` directly — not through the CLI — because that
is the contract a second surface will depend on.
"""

from datetime import date

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, core, db, queries
from strategy_store.models import Evidence, Factor, Position, Question, Revision


def _draft(title="Draft", element="ptw.box4", scope="fleet", status="holding"):
    with db.session() as s:
        p = Position(title=title, element_key=element, status=status, scope=scope,
                     source_system="manual", source_ref=f"t-{title.lower().replace(' ', '-')}")
        s.add(p)
        s.commit()
        return p.id


# ─────────────────────────────────────────────────────── the three outcomes


def test_adopt_dates_it_and_leaves_a_trail(store):
    pid = _draft()
    with db.session() as s:
        core.adopt(s, [pid], when=date(2026, 8, 28), note="because it is true")
        s.commit()
    with db.session() as s:
        p = s.get(Position, pid)
        assert p.decided_on == date(2026, 8, 28)
        ev = [e for e in p.evidence if e.source == "adopt"]
        assert len(ev) == 1 and ev[0].summary == "because it is true"
        assert ev[0].stance == "supports"


def test_reject_retires_and_dates_so_it_leaves_the_pile(store):
    """A rejection is a decision. Undated, it would sit in `pending` forever
    looking like work nobody had got to."""
    pid = _draft()
    with db.session() as s:
        core.reject(s, [pid], when=date(2026, 8, 28), note="the premise is wrong")
        s.commit()
    with db.session() as s:
        p = s.get(Position, pid)
        assert p.status == "retired" and p.decided_on == date(2026, 8, 28)
        assert queries.pending(s) == []
        assert [e.stance for e in p.evidence if e.source == "reject"] == ["opposes"]


def test_reject_without_a_reason_is_refused(store):
    pid = _draft()
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="needs a reason"):
            core.reject(s, [pid], note=None)


def test_reject_and_return_are_not_the_same_state(store):
    """`return` = not ready (still pending, still held-as-draft).
    `reject` = not held (retired, dated, gone from the pile)."""
    a, b = _draft("A"), _draft("B")
    with db.session() as s:
        core.adopt(s, [a])
        s.commit()
        core.send_back(s, [a], note="premature")
        core.reject(s, [b], note="no")
        s.commit()
    with db.session() as s:
        assert s.get(Position, a).decided_on is None
        assert s.get(Position, a).status == "holding"
        assert s.get(Position, b).status == "retired"
        assert [p.id for p in queries.pending(s)] == [a]


def test_a_bad_id_writes_nothing(store):
    """Validate every target before mutating any — through core, not just the CLI."""
    good = _draft()
    with db.session() as s:
        with pytest.raises(core.InvariantError):
            core.adopt(s, [good, 99999])
        s.rollback()
    with db.session() as s:
        assert s.get(Position, good).decided_on is None


# ────────────────────────────────────────────────────── store-owned columns


def test_set_fields_refuses_a_column_that_belongs_to_nobody(store):
    """A generic row update would walk past the boundary a sync depends on; core
    names the writable columns instead."""
    pid = _draft()
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="not a store-owned column"):
            core.set_fields(s, pid, source_ref="rewritten by a surface")


def _synced_draft(title="Synced", source_system="vikunja"):
    with db.session() as s:
        p = Position(title=title, element_key="ptw.box4", status="holding",
                     scope="fleet", source_system=source_system,
                     source_ref="#0003")
        s.add(p)
        s.commit()
        return p.id


def test_set_fields_refuses_text_on_a_synced_position(store):
    """vikunja and beads own their text: the next sync would overwrite anything
    typed here, so the edit would be silently lost rather than rejected."""
    pid = _synced_draft()
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="would be overwritten on the next sync"):
            core.set_fields(s, pid, statement="rewritten by a surface")


def test_set_fields_writes_text_on_a_manual_position(store):
    """A manual position has no upstream, so nothing but the store owns its text.
    This is what lets a bloated statement be cut back to the claim, with the case
    for it moved into factors and evidence."""
    pid = _draft()
    with db.session() as s:
        core.set_fields(s, pid, statement="One sentence.", title="Retitled")
        s.commit()
    with db.session() as s:
        p = s.get(Position, pid)
        assert p.statement == "One sentence." and p.title == "Retitled"


def test_set_fields_writes_the_columns_it_does_own(store):
    pid = _draft()
    with db.session() as s:
        core.set_fields(s, pid, status="contested", element_key="ptw.box5")
        s.commit()
    with db.session() as s:
        p = s.get(Position, pid)
        assert p.status == "contested" and p.element_key == "ptw.box5"


# ───────────────────────────────────────────────────────────── supersession


def test_narrower_scope_cannot_silently_supersede_a_wider_one(store):
    wide, narrow = _draft("Wide", scope="fleet"), _draft("Narrow", scope="single-rig")
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="no new evidence named"):
            core.supersede(s, narrow, wide)
        s.rollback()
    with db.session() as s:
        core.supersede(s, narrow, wide, evidence_note="the rig proved it")
        s.commit()
        assert s.get(Position, wide).superseded_by_id == narrow


# ──────────────────────────────────────────── factors, evidence, questions


def test_evidence_stance_is_validated_and_optional(store):
    pid = _draft()
    with db.session() as s:
        core.add_evidence(s, pid, source="room", summary="no stance recorded")
        core.add_evidence(s, pid, source="room", summary="cuts for", stance="supports")
        s.commit()
        with pytest.raises(core.InvariantError, match="stance must be one of"):
            core.add_evidence(s, pid, source="x", summary="y", stance="pro")


def test_unstated_stance_is_not_counted_as_agreement(store):
    """The 53 rows written before stance existed must not be back-filled into
    'supports' — an unstated stance is not agreement."""
    pid = _draft()
    with db.session() as s:
        core.add_evidence(s, pid, source="old", summary="observed, no stance")
        s.commit()
        d = queries.dossier(s, pid)
        assert d["evidence"]["supports"] == []
        assert len(d["evidence"]["unstated"]) == 1
        assert d["bare"] is True


def test_a_position_with_a_case_is_not_bare(store):
    pid = _draft()
    with db.session() as s:
        core.add_factor(s, pid, kind="for", text="it is cheap")
        s.commit()
        assert queries.dossier(s, pid)["bare"] is False


def test_factor_kind_is_validated(store):
    pid = _draft()
    with db.session() as s:
        with pytest.raises(core.InvariantError, match="kind must be one of"):
            core.add_factor(s, pid, kind="maybe", text="…")


def test_an_open_question_blocks_but_does_not_decide(store):
    pid = _draft()
    with db.session() as s:
        core.ask(s, pid, text="what does the chapter actually pay?")
        s.commit()
    with db.session() as s:
        assert s.get(Position, pid).decided_on is None       # still pending
        assert len(core.open_questions(s, pid)) == 1          # but blocked
        assert queries.dossier(s, pid)["open_questions"]


def test_answering_unblocks_and_cannot_be_overwritten(store):
    pid = _draft()
    with db.session() as s:
        q = core.ask(s, pid, text="how much?")
        s.commit()
        qid = q.id
    with db.session() as s:
        core.answer(s, qid, text="five figures")
        s.commit()
    with db.session() as s:
        assert core.open_questions(s, pid) == []
        with pytest.raises(core.InvariantError, match="was answered on"):
            core.answer(s, qid, text="something else")


# ─────────────────────────────────────────────────────────── the triage loop


def test_triage_queue_puts_blocked_positions_first_and_bare_ones_last(store):
    bare = _draft("Bare")
    argued = _draft("Argued")
    blocked = _draft("Blocked")
    with db.session() as s:
        core.add_factor(s, argued, kind="against", text="it costs a lot")
        core.ask(s, blocked, text="unknown")
        s.commit()
        assert [p.id for p in queries.triage_queue(s)] == [blocked, argued, bare]


def test_triage_queue_includes_a_dated_dispute_and_a_dated_blocked_row(store):
    """`pending` means undated, but an undated draft is not the only open issue.
    A contested position carries a date and is still a live dispute; a dated
    position with an open question is unresolved by construction. Walking only
    the pending pile skipped #49 — contested, dated, gate three days out."""
    from datetime import date as _date

    settled = _draft("Settled", status="resolved")
    dispute = _draft("Dispute", status="contested")
    blocked = _draft("Blocked but dated")
    with db.session() as s:
        for pid in (settled, dispute, blocked):
            s.get(Position, pid).decided_on = _date(2026, 8, 23)
        core.ask(s, blocked, text="unknown")
        s.commit()
        ids = [p.id for p in queries.triage_queue(s)]
        assert dispute in ids and blocked in ids
        assert settled not in ids
        assert queries.triage_queue(s, pending_only=True) == []


def test_a_gate_pulls_its_position_to_the_front(store):
    """Order is what a date forces first — judgement second."""
    from datetime import date as _date

    from strategy_store.models import Forcing

    argued = _draft("Argued")
    gated = _draft("Gated")
    with db.session() as s:
        core.add_factor(s, argued, kind="for", text="already has a case")
        s.add(Forcing(title="due soon", condition="c", gates="a decision",
                      due_on=_date(2026, 8, 31), position_id=gated, source_ref="g-soon"))
        s.commit()
        assert [p.id for p in queries.triage_queue(s)][0] == gated


def test_a_retired_position_is_not_an_open_issue(store):
    pid = _draft()
    with db.session() as s:
        core.reject(s, [pid], note="no")
        s.commit()
        assert queries.triage_queue(s) == []


def test_triage_adopts_from_the_prompt(store):
    pid = _draft()
    r = CliRunner().invoke(cli.main, ["triage", "--id", str(pid)], input="a\nheld\n")
    assert r.exit_code == 0, r.output
    with db.session() as s:
        assert s.get(Position, pid).decided_on is not None
    assert "1 adopted" in r.output


def test_triage_records_a_question_and_leaves_it_pending(store):
    pid = _draft()
    r = CliRunner().invoke(cli.main, ["triage", "--id", str(pid)],
                           input="q\nwhat would the chapter pay?\n")
    assert r.exit_code == 0, r.output
    with db.session() as s:
        assert s.get(Position, pid).decided_on is None
        assert [q.text for q in core.open_questions(s, pid)] == ["what would the chapter pay?"]
    assert "flagged" in r.output


def test_triage_shows_the_case_it_has(store):
    pid = _draft("Argued")
    with db.session() as s:
        core.add_factor(s, pid, kind="for", text="the rigs already prove it")
        core.add_factor(s, pid, kind="test", text="install it on someone else's box")
        core.add_evidence(s, pid, source="room", summary="a buyer asked for it",
                          stance="supports")
        s.commit()
    r = CliRunner().invoke(cli.main, ["triage", "--id", str(pid)], input="s\n")
    assert "the rigs already prove it" in r.output
    assert "WOULD SETTLE IT" in r.output
    assert "install it on someone else's box" in r.output
    assert "EVIDENCE FOR" in r.output


def test_triage_flags_a_position_with_no_case_at_all(store):
    pid = _draft("Bare")
    r = CliRunner().invoke(cli.main, ["triage", "--id", str(pid)], input="s\n")
    assert "no case recorded either way" in r.output


def test_triage_renders_a_refusal_and_keeps_going(store):
    """A refused write is shown and the loop continues — a surface never gets to
    work around an invariant, but it must not fall over on one either."""
    pid = _draft("Already gone", status="retired")
    r = CliRunner().invoke(
        cli.main, ["triage", "--id", str(pid)], input="r\nchanged my mind\ns\n"
    )
    assert r.exit_code == 0
    assert "refused: #" in r.output and "already retired" in r.output
    assert "1 skipped" in r.output


def test_cli_reject_and_factor_and_ask_round_trip(store):
    pid = _draft()
    run = CliRunner().invoke
    r = run(cli.main, ["factor", str(pid), "--kind", "against", "--text", "too early"])
    assert r.exit_code == 0
    assert run(cli.main, ["ask", str(pid), "how many rooms?"]).exit_code == 0
    assert "how many rooms?" in run(cli.main, ["questions"]).output
    assert run(cli.main, ["reject", str(pid), "--note", "not ours"]).exit_code == 0
    with db.session() as s:
        assert s.get(Position, pid).status == "retired"
        assert len(list(s.scalars(select(Factor)))) == 1
        assert len(list(s.scalars(select(Question)))) == 1
        assert any(e.source == "reject" for e in s.scalars(select(Evidence)))


# ─────────────────────────────────────────────── revisions: the pass is reversible


def test_rewriting_a_statement_snapshots_what_it_replaced(store):
    """A compression pass over the cascade is a lossy edit made in bulk. The
    snapshot is what makes a bad summary recoverable rather than authoritative."""
    pid = _draft()
    with db.session() as s:
        core.set_fields(s, pid, statement="The long original, with all the reasoning.")
        s.commit()
    with db.session() as s:
        core.set_fields(s, pid, statement="The claim.", note="cut back to the claim")
        s.commit()
    with db.session() as s:
        p = s.get(Position, pid)
        assert p.statement == "The claim."
        assert len(p.revisions) == 2
        latest = max(p.revisions, key=lambda r: r.id)
        assert latest.statement == "The long original, with all the reasoning."
        assert latest.changed == "statement"
        assert latest.note == "cut back to the claim"


def test_a_write_that_does_not_change_text_snapshots_nothing(store):
    pid = _draft()
    with db.session() as s:
        core.set_fields(s, pid, statement="Same.")
        s.commit()
    with db.session() as s:
        core.set_fields(s, pid, statement="Same.", status="contested")
        s.commit()
    with db.session() as s:
        p = s.get(Position, pid)
        assert p.status == "contested"
        assert len(p.revisions) == 1


def test_revisions_command_reports_when_there_are_none(store):
    pid = _draft()
    res = CliRunner().invoke(cli.main, ["revisions", str(pid)])
    assert res.exit_code == 0
    assert "never been rewritten" in res.output


# ─────────────────────────────────────────────────────── gates that can be closed


def _gate(title="A gate", due=None, ref="t-gate"):
    from strategy_store.models import Forcing
    with db.session() as s:
        f = Forcing(title=title, condition="checkable", gates="something downstream",
                    source_system="manual", source_ref=ref, due_on=due)
        s.add(f)
        s.commit()
        return f.id


def test_gate_resolve_closes_it_and_says_what_it_was_holding(store):
    from strategy_store.models import Forcing
    gid = _gate()
    res = CliRunner().invoke(cli.main, ["gate", "resolve", str(gid), "--note", "answered by #88"])
    assert res.exit_code == 0, res.output
    assert "was holding: something downstream" in res.output
    with db.session() as s:
        f = s.get(Forcing, gid)
        assert f.resolved_on is not None
        assert "answered by #88" in f.gates


def test_gate_resolve_refuses_one_already_closed(store):
    """Re-resolving would silently move the date and lose when it was actually
    answered."""
    gid = _gate(ref="t-gate-2")
    CliRunner().invoke(cli.main, ["gate", "resolve", str(gid)])
    res = CliRunner().invoke(cli.main, ["gate", "resolve", str(gid)])
    assert res.exit_code != 0
    assert "already resolved" in res.output


def test_gate_resolve_validates_every_id_before_closing_any(store):
    """Half-applying a multi-gate resolve would leave the caller unsure which
    closed."""
    from strategy_store.models import Forcing
    gid = _gate(ref="t-gate-3")
    res = CliRunner().invoke(cli.main, ["gate", "resolve", str(gid), "999999"])
    assert res.exit_code != 0
    with db.session() as s:
        assert s.get(Forcing, gid).resolved_on is None


def test_gate_reopen_undoes_a_resolve(store):
    from strategy_store.models import Forcing
    gid = _gate(ref="t-gate-4")
    CliRunner().invoke(cli.main, ["gate", "resolve", str(gid)])
    res = CliRunner().invoke(cli.main, ["gate", "reopen", str(gid)])
    assert res.exit_code == 0
    with db.session() as s:
        assert s.get(Forcing, gid).resolved_on is None


# ────────────────────────────────────────────────── clarity: readable by a stranger


def _stated(text, title="A position", ref="t-clar"):
    pid = _draft(title=title)
    with db.session() as s:
        core.set_fields(s, pid, statement=text)
        s.commit()
    return pid


def test_clarity_passes_a_claim_in_one_sentence(store):
    _stated("Ivan is a speaker who builds solutions in front of an audience.",
            title="A speaker who builds solutions in front of an audience", ref="t-c1")
    with db.session() as s:
        assert core.clarity(s) == []


def test_clarity_flags_a_statement_that_absorbed_its_own_reasoning(store):
    pid = _stated(" ".join(["word"] * 60), title="Word word word word", ref="t-c2")
    with db.session() as s:
        rows = core.clarity(s)
    assert [r["id"] for r in rows] == [pid]
    assert "long" in {k for k, _ in rows[0]["fails"]}


def test_clarity_flags_a_statement_that_cites_the_store_to_explain_itself(store):
    """Unreadable without the store open beside you — the reader a position has
    to survive is a stranger."""
    pid = _stated("This follows from #23 and settles the box 2 question.",
                  title="This follows and settles the question", ref="t-c3")
    with db.session() as s:
        rows = core.clarity(s)
    assert "self-reference" in {k for k, _ in next(r for r in rows if r["id"] == pid)["fails"]}


def test_clarity_flags_retired_phrasing(store):
    pid = _stated("Proving the method is the boss, as it has been for two years.",
                  title="Proving the method is the boss", ref="t-c4")
    with db.session() as s:
        rows = core.clarity(s)
    assert "jargon" in {k for k, _ in next(r for r in rows if r["id"] == pid)["fails"]}


def test_clarity_flags_a_headline_that_drifted_from_the_claim(store):
    pid = _stated("Seats are sold retail or wholesale and nothing else counts.",
                  title="Elephants migrate through burgundy telephone cabinets", ref="t-c5")
    with db.session() as s:
        rows = core.clarity(s)
    assert "drift" in {k for k, _ in next(r for r in rows if r["id"] == pid)["fails"]}


def test_clarity_marks_synced_positions_as_not_fixable_here(store):
    pid = _synced_draft(title="Synced and wordy")
    with db.session() as s:
        p = s.get(Position, pid)
        p.statement = " ".join(["word"] * 60)
        s.commit()
    with db.session() as s:
        row = next(r for r in core.clarity(s) if r["id"] == pid)
    assert row["editable"] is False


def test_many_positions_can_be_superseded_by_one(store):
    """A box collapsing from eighteen positions to five is the normal case, not an
    edge one. The old schema put the link on the SUCCESSOR, so each supersede
    overwrote the last and silently left the earlier ones marked current."""
    a, b, c = _draft("A"), _draft("B"), _draft("C")
    survivor = _draft("Consolidated")
    with db.session() as s:
        for old in (a, b, c):
            core.supersede(s, survivor, old)
        s.commit()
    with db.session() as s:
        for old in (a, b, c):
            assert s.get(Position, old).superseded_by_id == survivor
        assert {p.id for p in s.get(Position, survivor).supersedes} == {a, b, c}
        assert not ({a, b, c} & {p.id for p in queries.current_positions(s)})
