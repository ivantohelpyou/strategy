"""`strategy export --ctx <dir>` — positions as a ctx decisions corpus (hq-sl5k).

Fixture positions only: titles and statements below are synthetic, invented
for this test, never real names or real judgments (this repo goes public
2026-10-09; see 71fa6fb).
"""

from datetime import date

import pytest
import yaml
from click.testing import CliRunner

from strategy_store import cli, ctx_export
from strategy_store.models import Evidence, Position


@pytest.fixture
def out_dir(tmp_path):
    """A dedicated export target, never bare `tmp_path` — the `isolated_store`
    fixture (conftest.py, autouse) already writes `isolated.db` straight into
    `tmp_path`, and `_validate_target_dir` correctly refuses to write a ctx
    export on top of an unrelated file it doesn't recognize."""
    return tmp_path / "decisions"


def _p(store, **kw):
    kw.setdefault("scope", "web-chat")
    kw.setdefault("status", "holding")
    kw.setdefault("source_system", "manual")
    with store.session() as s:
        p = Position(**kw)
        s.add(p)
        s.commit()
        return p.id


def _parse(path):
    text = path.read_text(encoding="utf-8")
    _, fm_text, body = text.split("---", 2)
    return yaml.safe_load(fm_text), body


def test_file_count_matches_positions(store, out_dir):
    _p(store, title="Widgets before gadgets", statement="Ship widgets first.", source_ref="a")
    _p(store, title="Gadgets can wait", statement="Gadgets are not the priority.", source_ref="b")
    stats = ctx_export.export_ctx(out_dir)
    assert stats.written == 2
    md_files = sorted(out_dir.glob("pos-*.md"))
    assert len(md_files) == 2
    assert (out_dir / "README.md").exists()


def test_byte_identical_on_second_export(store, out_dir):
    _p(store, title="Widgets before gadgets", statement="Ship widgets first.", source_ref="a")
    ctx_export.export_ctx(out_dir)
    before = {p.name: p.read_bytes() for p in out_dir.glob("*.md")}
    ctx_export.export_ctx(out_dir)
    after = {p.name: p.read_bytes() for p in out_dir.glob("*.md")}
    assert before == after


def test_stale_file_removed_when_position_leaves_store(store, out_dir):
    a = _p(store, title="Widgets before gadgets", statement="Ship widgets first.", source_ref="a")
    _p(store, title="Gadgets can wait", statement="Gadgets are not the priority.", source_ref="b")
    stats = ctx_export.export_ctx(out_dir)
    assert stats.written == 2
    stale_name = ctx_export.ctx_id(a) + ".md"
    assert (out_dir / stale_name).exists()

    with store.session() as s:
        row = s.get(Position, a)
        s.delete(row)
        s.commit()

    stats = ctx_export.export_ctx(out_dir)
    assert stats.written == 1
    assert stale_name in stats.removed
    assert not (out_dir / stale_name).exists()


def test_reciprocal_supersession_emitted(store, out_dir):
    old_id = _p(
        store, title="Old plan", statement="The old plan.", source_ref="old", status="retired"
    )
    new_id = _p(
        store, title="New plan", statement="The new plan.", source_ref="new", status="resolved"
    )
    with store.session() as s:
        old = s.get(Position, old_id)
        old.superseded_by_id = new_id
        s.commit()

    ctx_export.export_ctx(out_dir)
    old_fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(old_id)}.md")
    new_fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(new_id)}.md")

    assert old_fm["superseded-by"] == [ctx_export.ctx_id(new_id)]
    assert new_fm["supersedes"] == [ctx_export.ctx_id(old_id)]


def test_status_mapping(store, out_dir):
    ids = {
        status: _p(
            store,
            title=f"{status} position",
            statement=f"A {status} position.",
            source_ref=status,
            status=status,
        )
        for status in ("resolved", "holding", "contested", "retired")
    }
    ctx_export.export_ctx(out_dir)
    for status, pid in ids.items():
        fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(pid)}.md")
        assert fm["status"] == ctx_export.STATUS_MAP[status]
    assert ctx_export.STATUS_MAP == {
        "resolved": "accepted",
        "holding": "proposed",
        "contested": "contested",
        "retired": "retired",
    }


def test_resolved_but_superseded_maps_to_superseded_not_accepted(store, out_dir):
    """`core.supersede()` never touches `status` (models.py), so a `resolved`
    position can sit resolved in the store years after something replaced it
    — true in the real store, on the first real export (dozens of rows). If
    that were exported as `status: accepted`, ctx lint's decisions rule
    ("an accepted doc that something else claims to supersede is an error")
    would fire on every one of them."""
    old_id = _p(
        store,
        title="Old, still resolved",
        statement="Still says resolved.",
        source_ref="old",
        status="resolved",
    )
    new_id = _p(
        store,
        title="New replacement",
        statement="What replaced it.",
        source_ref="new",
        status="resolved",
    )
    with store.session() as s:
        old = s.get(Position, old_id)
        old.superseded_by_id = new_id
        s.commit()

    ctx_export.export_ctx(out_dir)
    old_fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(old_id)}.md")
    new_fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(new_id)}.md")
    assert old_fm["status"] == "superseded"
    assert new_fm["status"] == "accepted"


def test_readme_carries_no_decision_frontmatter(store, out_dir):
    """The README is not a decision — ctx's `exclude: ["README.md"]` default
    (hq-sl5k, ctx side) keeps it out of indexing/lint entirely, so it no
    longer needs fake frontmatter to pass `ctx lint` (review finding). Only
    the generator marker `_validate_target_dir` checks for, plus prose."""
    ctx_export.export_ctx(out_dir)
    text = (out_dir / "README.md").read_text(encoding="utf-8")
    assert text.startswith(ctx_export.GENERATOR_MARKER)
    assert "id:" not in text
    assert "status:" not in text
    assert not text.lstrip().startswith("---")  # no frontmatter block at all


def test_missing_one_liner_omits_summary(store, out_dir):
    _p(
        store,
        title="Multi-paragraph position",
        statement="First paragraph, one sentence.\n\nSecond paragraph, ignored.",
        source_ref="multi",
    )
    _p(store, title="No statement at all", statement="", source_ref="blank")
    _p(store, title="Clean one-liner", statement="A single clear sentence.", source_ref="clean")

    stats = ctx_export.export_ctx(out_dir)
    assert len(stats.missing_summary) == 1  # only the blank one

    found_with_summary = 0
    found_without_summary = 0
    for md in sorted(out_dir.glob("pos-*.md")):
        fm, _ = _parse(md)
        if "summary" in fm:
            found_with_summary += 1
        else:
            found_without_summary += 1
    assert found_with_summary == 2
    assert found_without_summary == 1


def test_hard_wrapped_single_paragraph_recovers_a_summary(store, out_dir):
    """The bug the review caught: a hard-wrapped but single-paragraph
    statement used to lose its summary just for containing a newline."""
    pid = _p(
        store,
        title="Hard-wrapped",
        statement="This continues\nacross more than\none physical line.",
        source_ref="wrapped",
    )
    ctx_export.export_ctx(out_dir)
    fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(pid)}.md")
    assert fm["summary"] == "This continues across more than one physical line."


def test_two_paragraph_statement_summary_is_first_paragraph_only(store, out_dir):
    pid = _p(
        store,
        title="Two paragraphs",
        statement="First paragraph here.\n\nSecond paragraph, not part of the summary.",
        source_ref="twopara",
    )
    ctx_export.export_ctx(out_dir)
    fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(pid)}.md")
    assert fm["summary"] == "First paragraph here."


def test_crlf_two_paragraph_statement_summary_is_first_paragraph_only(store, out_dir):
    """Review finding (hq-sl5k round 2, reproduced): `\\n[ \\t]*\\n` misses a
    CRLF blank line (`\\r\\n\\r\\n`), so a CRLF two-paragraph statement used to
    export both paragraphs into `summary`."""
    pid = _p(
        store,
        title="CRLF two paragraphs",
        statement="First paragraph here.\r\n\r\nSecond paragraph, not part of the summary.",
        source_ref="crlf",
    )
    ctx_export.export_ctx(out_dir)
    fm, _ = _parse(out_dir / f"{ctx_export.ctx_id(pid)}.md")
    assert fm["summary"] == "First paragraph here."


def test_evidence_and_tags_and_date_in_body_and_frontmatter(store, out_dir):
    pid = _p(
        store,
        title="Evidenced position",
        statement="A position with evidence.",
        source_ref="ev",
        venture="widgetco",
        element_key="ptw.box2",
        decided_on=date(2026, 8, 1),
        status="resolved",
    )
    with store.session() as s:
        s.add(
            Evidence(
                position_id=pid,
                source="a test observation",
                scope="single-rig",
                summary="it happened",
                observed_on=date(2026, 8, 2),
            )
        )
        s.commit()

    ctx_export.export_ctx(out_dir)
    fm, body = _parse(out_dir / f"{ctx_export.ctx_id(pid)}.md")
    assert fm["date"] == "2026-08-01"
    assert fm["tags"] == ["widgetco", "ptw.box2"]
    assert fm["terms"] == []
    assert "## Evidence" in body
    assert "2026-08-02" in body
    assert "it happened" in body


def test_cli_export_ctx_flag(store, out_dir):
    _p(store, title="CLI-driven position", statement="Reached via the CLI flag.", source_ref="c1")
    r = CliRunner().invoke(cli.main, ["export", "--ctx", str(out_dir)])
    assert r.exit_code == 0, r.output
    assert "exported 1 positions" in r.output
    assert len(list(out_dir.glob("pos-*.md"))) == 1


def test_cli_export_ctx_and_out_together_is_refused(store, out_dir, tmp_path):
    r = CliRunner().invoke(
        cli.main, ["export", "--ctx", str(out_dir), "--out", str(tmp_path / "positions.jsonl")]
    )
    assert r.exit_code != 0
    assert "--ctx" in r.output and "--out" in r.output
    assert not out_dir.exists()


def test_target_dir_refuses_unrelated_non_empty_directory(store, out_dir):
    out_dir.mkdir(parents=True)
    (out_dir / "notes.txt").write_text("someone else's file")
    with pytest.raises(ctx_export.TargetDirNotOwnedError, match="notes.txt"):
        ctx_export.export_ctx(out_dir)
    # Refusal must write nothing: the unrelated file is untouched and no
    # export files appear.
    assert (out_dir / "notes.txt").read_text() == "someone else's file"
    assert not list(out_dir.glob("pos-*.md"))
    assert not (out_dir / "README.md").exists()


def test_target_dir_refuses_a_hand_written_readme(store, out_dir):
    """The exact bug the review reproduced: --ctx pointed at a directory that
    already had its own README.md must not clobber it."""
    out_dir.mkdir(parents=True)
    (out_dir / "README.md").write_text("# Someone's own notes\n\nNot generated by ctx export.\n")
    with pytest.raises(ctx_export.TargetDirNotOwnedError, match="README.md"):
        ctx_export.export_ctx(out_dir)
    assert "Someone's own notes" in (out_dir / "README.md").read_text()


def test_target_dir_upgrades_a_round1_shaped_export(store, out_dir):
    """A directory from the bdc0284-shape exporter (fake decision frontmatter,
    the old prose sentence) must be accepted and upgraded, not permanently
    refused just because it predates the marker."""
    out_dir.mkdir(parents=True)
    (out_dir / "pos-999.md").write_text("---\nid: pos-999\ntitle: Old\nsummary: old shape.\n---\n")
    (out_dir / "README.md").write_text(
        "---\nid: readme\ntitle: Generated by strategy export --ctx — do not edit\n"
        "summary: old shape readme.\nstatus: retired\nsupersedes: []\nsuperseded-by: []\n"
        "tags: []\nterms: []\n---\n\n# decisions\n\n"
        "Generated by `strategy export --ctx <dir>`. Do not edit these files — they are\n"
        "regenerated on every export from the strategy store.\n"
    )
    _p(store, title="Fresh position", statement="Replaces the old shape.", source_ref="fresh")

    stats = ctx_export.export_ctx(out_dir)  # must not raise

    assert stats.written == 1
    assert not (out_dir / "pos-999.md").exists()  # stale round-1 file removed
    upgraded = (out_dir / "README.md").read_text(encoding="utf-8")
    assert upgraded.startswith(ctx_export.GENERATOR_MARKER)
    assert "id:" not in upgraded


def test_target_dir_still_refuses_an_unrelated_readme(store, out_dir):
    """The upgrade path is narrow: a README that merely mentions ctx in
    passing, without the round-1 sentence, is still refused."""
    out_dir.mkdir(parents=True)
    (out_dir / "README.md").write_text("# Notes\n\nSomething about ctx, unrelated to export.\n")
    with pytest.raises(ctx_export.TargetDirNotOwnedError, match="README.md"):
        ctx_export.export_ctx(out_dir)


def test_target_dir_accepts_its_own_prior_export(store, out_dir):
    """A second export into a directory `export_ctx` itself produced earlier
    (pos-*.md files plus its own marked README) is the normal, expected case
    — not a conflict."""
    _p(store, title="First run", statement="Exists before the second export.", source_ref="r1")
    ctx_export.export_ctx(out_dir)
    # Runs again against the same, now-populated directory without raising.
    stats = ctx_export.export_ctx(out_dir)
    assert stats.written == 1


def test_target_dir_accepts_a_fresh_or_empty_directory(store, out_dir):
    ctx_export.export_ctx(out_dir)  # doesn't exist yet
    assert out_dir.exists()
    for f in list(out_dir.glob("*")):
        f.unlink()
    ctx_export.export_ctx(out_dir)  # exists but now empty
