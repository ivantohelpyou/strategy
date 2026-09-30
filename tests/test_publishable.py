"""The repo stays publishable: no real task ids, patent numbers or private names (st-lhu).

Two layers, like test_schema_contract:

* the mechanism, against synthetic text and a throwaway git repo. The residue is
  built at runtime (``f"vikunja:#{n}"``) so these fixtures never trip the live
  scan of this file.
* the live tree. Task ids and patents are checked everywhere. Private terms are
  checked where the private list exists and skipped where it does not — CI and
  anyone who clones the public repo have no such file, and should not.
"""

from __future__ import annotations

import subprocess

import pytest

from strategy_store import publishable as pub

REAL_ID = 3 * 1000 + 857  # any id outside the placeholder set


def _kinds(findings):
    return [(f.kind, f.match) for f in findings]


# --- the mechanism --------------------------------------------------------


@pytest.mark.parametrize("sep", [":#", " #", "#"])
def test_a_real_task_id_is_found_however_it_is_written(sep):
    text = f"four gates sit on Vikunja{sep}{REAL_ID}"
    assert _kinds(pub.scan_text(text, "f.py")) == [("task-id", f"Vikunja{sep}{REAL_ID}")]


def test_placeholder_ids_are_what_examples_are_for():
    text = "\n".join(f"vikunja:#{n}" for n in sorted(pub.PLACEHOLDER_TASK_IDS))
    assert pub.scan_text(text, "f.py") == []


def test_a_bare_four_digit_id_is_a_task_id():
    """How the report strings cited tasks: "(never says what is sold", #NNNN)."""
    found = pub.scan_text(f'an empty box ("never says what is sold", #{REAL_ID})', "f.py")
    assert _kinds(found) == [("task-id", f"#{REAL_ID}")]


def test_vikunja_task_spelled_out_is_found():
    found = pub.scan_text(f"see Vikunja task #{REAL_ID}", "f.py")
    assert _kinds(found) == [("task-id", f"Vikunja task #{REAL_ID}")]


@pytest.mark.parametrize(
    "text",
    ["position #112 says so", "an em dash &#8212; here", "doc.md#1234x", "color #1234ab"],
    ids=["position-id", "html-entity", "anchor", "hex-color"],
)
def test_things_shaped_like_ids_that_are_not(text):
    """`#112` alone is a store row, and store rows are how the docs cite positions."""
    assert pub.scan_text(text, "f.py") == []


def test_patent_numbers_are_found_by_shape():
    us = "63" + "/123,456"
    gb = "GB " + "1234567"
    found = pub.scan_text(f"filed as {us}, and {gb}", "f.py")
    assert _kinds(found) == [("patent", us), ("patent", gb)]


def test_private_terms_match_case_insensitively_and_carry_the_line():
    found = pub.scan_text("ok\nquoting JANE example here", "t.py", terms=["Jane Example"])
    assert [(f.where, f.kind, f.match) for f in found] == [
        ("t.py:2", "private-term", "Jane Example")
    ]


def test_the_term_file_is_optional_and_comments_are_not_terms(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_PRIVATE_TERMS", str(tmp_path / "absent.txt"))
    assert pub.load_private_terms() is None
    f = tmp_path / "terms.txt"
    f.write_text("# names\nJane Example  # a counterparty\n\n  Acme  \n")
    monkeypatch.setenv("STRATEGY_PRIVATE_TERMS", str(f))
    assert pub.load_private_terms() == ["Jane Example", "Acme"]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(repo, path, text, message):
    (repo / path).write_text(text)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)


def test_a_term_removed_from_head_is_still_in_the_history(repo):
    """The reason for --history: going public publishes every commit, not HEAD."""
    _commit(repo, "a.py", "# as Jane Example said\n", "quote")
    _commit(repo, "a.py", "# as someone said\n", "scrub")
    assert pub.scan_tree(repo, terms=["Jane Example"]) == []
    hist = pub.scan_history(repo, terms=["Jane Example"])
    assert _kinds(hist) == [("private-term", "Jane Example")]
    assert hist[0].where.endswith(":a.py")


def test_commit_messages_are_history_too(repo):
    """Close reasons get pasted into commit messages as often as into docstrings."""
    _commit(repo, "a.py", "x = 1\n", f"closes the gate on vikunja:#{REAL_ID}")
    hist = pub.scan_history(repo)
    assert _kinds(hist) == [("task-id", f"vikunja:#{REAL_ID}")]
    assert hist[0].where.endswith(":message")


def test_a_line_added_in_a_merge_is_history(repo):
    """A term typed into a conflict resolution is as public as any other line."""
    _commit(repo, "a.py", "x = 1\n", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "b.py", "y = 2\n", "side")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, "c.py", "z = 3\n", "main")
    _git(repo, "merge", "-q", "--no-commit", "side")
    (repo / "a.py").write_text("x = 1  # Jane Example resolved this\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "merge")
    hist = pub.scan_history(repo, terms=["Jane Example"])
    assert [(f.where.split(":")[1], f.match) for f in hist] == [("a.py", "Jane Example")]


def test_an_added_line_that_starts_like_a_header_is_content(repo):
    """`++ x` added is `+++ x` in the diff; it must not become a file path."""
    _commit(repo, "a.py", f"++ note\nvikunja:#{REAL_ID}\n", "tricky")
    hist = pub.scan_history(repo)
    assert [(f.where.split(":")[1], f.kind) for f in hist] == [("a.py", "task-id")]


# --- the live tree --------------------------------------------------------


def test_the_tree_has_no_real_task_ids_or_patent_numbers():
    found = pub.scan_tree()
    assert found == [], "\n".join(map(str, found))


def test_the_tree_has_no_private_terms():
    terms = pub.load_private_terms()
    if terms is None:
        pytest.skip(f"no private term list at {pub.private_terms_path()}")
    found = [f for f in pub.scan_tree(terms=terms) if f.kind == "private-term"]
    # never echo the term itself into a CI log: say where, not what
    assert found == [], "private terms at: " + ", ".join(f.where for f in found)
