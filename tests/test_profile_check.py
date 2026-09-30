"""sea-mii7.2 — the public surface, checked against what the store can license.

The case that prompted it: a 2026-08-20 LinkedIn rewrite drifted twice in one
pass — a cadence claim ("a free app a day") the drop count did not support, and
strategy-memo shorthand ("leave the publications behind") pasted into public
copy — plus two skills naming a segment the strategy had declined. The check
must catch exactly those, and go quiet once they are fixed.
"""

from datetime import date, timedelta

import pytest
import yaml
from click.testing import CliRunner
from sqlalchemy import select

from strategy_store import cli, profile
from strategy_store.models import Drop, Position, ProfileClaim, ProfileField

RULES = {
    "not_playing": {
        "AI Productivity": "vikunja:#0001",
        "Productivity Coaching": "vikunja:#0001",
    },
    "shorthand": {
        "leave the publications behind": "aspiration shorthand; in public it reads as "
        "abandoning the work"
    },
    "cadence": {"window_days": 30},
}


@pytest.fixture
def rules(tmp_path, monkeypatch):
    p = tmp_path / "profile-rules.yaml"
    p.write_text(yaml.safe_dump(RULES))
    monkeypatch.setenv("STRATEGY_PROFILE_RULES", str(p))
    monkeypatch.setenv("GT_ROOT", str(tmp_path / "no-registry"))
    return p


def _set(field, text, **kw):
    args = ["profile", "set", field, "--text", text]
    for k, v in kw.items():
        args += [f"--{k}", str(v)]
    r = CliRunner().invoke(cli.main, args)
    assert r.exit_code == 0, r.output
    return r


def _claim(field, text, *licenses):
    args = ["profile", "claim", field, "--text", text]
    for lic in licenses:
        args += ["--license", lic]
    r = CliRunner().invoke(cli.main, args)
    assert r.exit_code == 0, r.output
    return r


def _by_check(findings, prefix):
    return next(f for f in findings if f["check"].startswith(prefix))


def _run(store, surface="linkedin", today=None):
    with store.session() as s:
        return profile.check(s, surface, today=today)


# ------------------------------------------------------------------ the case


def test_the_2026_08_20_rewrite_fails_on_exactly_the_drifts_ivan_caught(store, rules):
    """One pass, four defects: a cadence word the drops do not support, memo
    shorthand in public copy, and two skills naming a declined segment."""
    today = date(2026, 8, 20)
    with store.session() as s:
        # Eight drops in the trailing 30 days — nowhere near one a day.
        for i in range(8):
            s.add(
                Drop(
                    posted_on=today - timedelta(days=i * 3),
                    url=f"https://example.invalid/{i}",
                    title=f"drop {i}",
                    format="post",
                )
            )
        s.commit()

    _set("headline", "I ship a free app a day and leave the publications behind.")
    _set("skill", "AI Productivity · Productivity Coaching · Schema evolution")

    findings = _run(store, today=today)
    cadence = _by_check(findings, "Cadence words")
    assert len(cadence["rows"]) == 1 and "a day" in cadence["rows"][0]
    assert "8 drops in 30 days" in cadence["rows"][0]

    shorthand = _by_check(findings, "Strategy-memo shorthand")
    assert len(shorthand["rows"]) == 1
    assert "leave the publications behind" in shorthand["rows"][0]

    not_playing = _by_check(findings, "Copy that advertises")
    assert len(not_playing["rows"]) == 2, "both off-strategy skills"
    assert all("#0001" in r for r in not_playing["rows"])


def test_and_goes_quiet_once_the_copy_is_fixed(store, rules):
    today = date(2026, 8, 20)
    _set("headline", "I build working database apps in public, and publish how.")
    _set("skill", "Schema evolution · Python · Live facilitation")
    for name in ("Cadence words", "Strategy-memo shorthand", "Copy that advertises"):
        assert _by_check(_run(store, today=today), name)["rows"] == []


# ---------------------------------------------------------------- the checks


def test_a_claim_with_no_licence_is_recorded_and_reported(store, rules):
    """Recorded as unlicensed rather than refused — refusing it would just move
    the unsupported claim somewhere the check cannot see."""
    _set("about", "182 ms across 12 phones.")
    out = _claim("about", "182 ms across 12 phones").output
    assert "UNLICENSED" in out
    rows = _by_check(_run(store), "Claims with nothing behind them")["rows"]
    assert len(rows) == 1 and "182 ms" in rows[0]


def test_a_licensed_claim_passes(store, rules):
    _set("about", "182 ms across 12 phones.")
    _claim("about", "182 ms across 12 phones", "position:20")
    assert _by_check(_run(store), "Claims with nothing behind them")["rows"] == []


def test_a_claim_licensed_by_a_superseded_position_is_flagged(store, rules):
    """The claim may still be true; its licence is not."""
    with store.session() as s:
        old = Position(title="old way", source_system="manual", source_ref="a", scope="fleet")
        s.add(old)
        s.flush()
        s.add(
            Position(
                title="new way", source_system="manual", source_ref="b", scope="fleet",
                supersedes_id=old.id,
            )
        )
        s.commit()
        old_id = old.id
    _set("about", "We do it the old way.")
    _claim("about", "the old way", f"position:{old_id}")
    rows = _by_check(_run(store), "Claims licensed by a decision")["rows"]
    assert len(rows) == 1 and f"#{old_id}" in rows[0]


def test_the_rules_dependent_checks_say_they_could_not_run_rather_than_passing(store, tmp_path,
                                                                              monkeypatch):
    """A check that cannot see its rules has not cleared anything."""
    monkeypatch.setenv("STRATEGY_PROFILE_RULES", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("GT_ROOT", str(tmp_path / "no-registry"))
    _set("headline", "I ship a free app a day.")
    findings = _run(store)
    for name in ("Copy that advertises", "Strategy-memo shorthand", "Products named"):
        f = _by_check(findings, name)
        assert f["ran"] is False and f["rows"] == []
    # The checks that need no rules still run.
    assert _by_check(findings, "Cadence words")["ran"] is True


def test_the_registry_check_names_what_ships_and_is_never_mentioned(store, rules, tmp_path,
                                                                    monkeypatch):
    import json

    gt = tmp_path / "gt"
    gt.mkdir()
    (gt / "apps.json").write_text(
        json.dumps(
            {
                "apps": {
                    "purr": {"status": "stable", "prod": {"url": "https://purr.invalid"}},
                    "halfbuilt": {"status": "wip"},
                }
            }
        )
    )
    monkeypatch.setenv("GT_ROOT", str(gt))
    _set("about", "I built halfbuilt, which you can try today.")
    rows = _by_check(_run(store), "Products named")["rows"]
    assert any("halfbuilt" in r and "not stable" in r for r in rows)
    assert any("purr" in r and "never named" in r for r in rows)


# ---------------------------------------------------------------- write path


def test_export_reproduces_the_fields_with_counts_and_flags_an_overrun(store, rules):
    _set("headline", "x" * 230, limit=220)
    _set("about", "short", ordinal=2)
    with store.session() as s:
        text = profile.export(s)
    assert "## headline [230/220]" in text and "⚠ OVER" in text
    assert text.index("## headline") < text.index("## about"), "surface order, not insertion order"


def test_setting_a_field_twice_updates_it_rather_than_duplicating(store, rules):
    _set("headline", "first")
    _set("headline", "second")
    with store.session() as s:
        rows = list(s.scalars(select(ProfileField)))
    assert len(rows) == 1 and rows[0].text == "second"


def test_the_default_char_limit_comes_from_the_surface(store, rules):
    _set("headline", "x")
    with store.session() as s:
        assert s.scalar(select(ProfileField)).char_limit == 220


def test_claiming_against_an_unrecorded_field_is_refused(store, rules):
    r = CliRunner().invoke(cli.main, ["profile", "claim", "about", "--text", "x"])
    assert r.exit_code != 0 and "profile set" in r.output
    with store.session() as s:
        assert list(s.scalars(select(ProfileClaim))) == []


def test_check_exits_non_zero_so_it_can_gate_a_paste(store, rules):
    _set("skill", "AI Productivity")
    assert CliRunner().invoke(cli.main, ["profile", "check"]).exit_code == 1
    _set("skill", "Schema evolution")
    assert CliRunner().invoke(cli.main, ["profile", "check"]).exit_code == 0


def test_an_empty_surface_reports_that_it_is_empty_not_seven_findings(store, rules):
    """"Nothing recorded" is not the same as "nothing wrong" — and findings
    about an empty profile are findings about this store, not the surface."""
    findings = _run(store)
    assert len(findings) == 1
    assert findings[0]["check"] == "Nothing recorded for this surface"
    assert findings[0]["ran"] is False
    assert CliRunner().invoke(cli.main, ["profile", "check"]).exit_code == 0


def test_a_short_registry_key_does_not_match_inside_an_ordinary_word(store, rules, tmp_path,
                                                                     monkeypatch):
    """Registry keys are short — "sos", "hib", "elevator". Substring matching
    reports them as named by any prose that happens to contain the letters."""
    import json

    gt = tmp_path / "gt"
    gt.mkdir()
    (gt / "apps.json").write_text(
        json.dumps({"apps": {"hib": {"status": "stable", "prod": {"url": "https://h.invalid"}}}})
    )
    monkeypatch.setenv("GT_ROOT", str(gt))
    _set("about", "Nothing here prohibits anything.")
    rows = _by_check(_run(store), "Products named")["rows"]
    assert any("hib" in r and "never named" in r for r in rows)
    _set("about", "I built hib.")
    assert _by_check(_run(store), "Products named")["rows"] == []


def test_a_stable_tool_with_no_public_url_is_not_reported_as_missing(store, rules, tmp_path,
                                                                    monkeypatch):
    """An internal tool has no business on a public profile. Reporting it as
    missing is how a check earns being ignored."""
    import json

    gt = tmp_path / "gt"
    gt.mkdir()
    (gt / "apps.json").write_text(
        json.dumps(
            {
                "apps": {
                    "obs-overlay": {"status": "stable", "name": "OBS Overlay Control"},
                    "sos": {
                        "status": "stable",
                        "name": "Survey of Software — research site",
                        "prod": {"url": "https://research.invalid"},
                    },
                }
            }
        )
    )
    monkeypatch.setenv("GT_ROOT", str(gt))
    _set("about", "Nothing named here.")
    rows = _by_check(_run(store), "Products named")["rows"]
    assert [r for r in rows if "obs-overlay" in r] == []
    assert any("sos" in r and "never named" in r for r in rows)
    # ...and the human name counts: the profile says "Survey of Software", not "sos".
    _set("about", "We work it live against the Survey of Software.")
    assert _by_check(_run(store), "Products named")["rows"] == []
