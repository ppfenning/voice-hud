"""Public-hygiene sweep: this repo went from a private, internal-tooling
checkout to a public one, and the two classes of thing that must never
reappear in a tracked file are (1) anything secret-shaped — API keys,
bearer tokens, PEM private-key material — and (2) a small set of literal
identifiers tied to the old internal setup (its issue tracker's product
name, that tracker's API host, and the repo owner's real name — the public
GitHub handle `ppfenning` is fine and is not on this list).

This walks `git ls-files -z` (stdlib subprocess only, no pip dependency;
`-z` NUL-separates entries so a path containing bytes that `core.quotePath`
would otherwise quote/mangle still round-trips — decoded with
`errors="surrogateescape"`, the same scheme the OS/Python already use for
undecodable filesystem bytes, so a non-UTF-8 path is preserved rather than
raising) and reads each tracked text file once. It is a tree walk over the
current checkout, not a history grep, so it stays fast and says nothing
about whether a leaked value is still reachable in an old commit. It is
also only ever run by `pytest -q`; this repo's CI (.github/workflows/ci.yml)
has `paths-ignore: ['**.md', '.gitignore', 'LICENSE']` on both push and
pull_request, by deliberate, pre-existing, and separately-documented
design ("Docs-only pushes do not trigger a run"), so a docs-only change
does not run this sweep in CI. That is a repo-level CI-trigger decision
outside this test file's scope, not something fixed here; flagging it
here is the breadcrumb.

The denylist literals live in the sibling module
tests/_public_hygiene_denylist.py, not in a YAML/JSON config file — the
initiative's instruction that they stay literal Python strings is still
honored, they are just one file over so the exemption below only has to
cover that constant, not this file's own body (which still gets scanned
for secret-shaped strings like any other tracked file). See that module's
docstring for the corrected premise: two of its three entries genuinely
are not recoverable from this sanitized tree and stay TODO placeholders
pending the owner/reviewer's firsthand knowledge — while either is a
placeholder, test_no_tracked_file_contains_a_denylisted_term below skips
loudly rather than passing vacuously, and a green run must never be read
as "the denylist was checked and came up clean" until it has been. The
third entry, the owner's real name, is populated for real because it is
directly evidenced by this tracked repo's own LICENSE file, which is why
LICENSE is exempted from the denylist check specifically (DENYLIST_
EXEMPT_PATHS) rather than the whole denylist item being left a
placeholder: an MIT license's copyright line is required to name its
holder, so that occurrence is not a leak.

The matching logic is factored into find_secret_matches /
find_denylist_matches so it can be exercised directly against synthetic
input (see TestMatchersHaveTeeth below), and scan() takes the file list as
a parameter so a planted offender in a real temporary file can be run
through the *whole* read-from-disk-then-match pipeline (see
TestScanEndToEnd below), not just the matcher functions in isolation.
"""

from __future__ import annotations

import re
import subprocess
from importlib import util as importlib_util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()
DENYLIST_MODULE_PATH = (THIS_FILE.parent / "_public_hygiene_denylist.py").resolve()
LICENSE_PATH = (REPO_ROOT / "LICENSE").resolve()

SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
)

# Paths exempt from every matcher: holds the denylist constants this
# sweep looks for, so it would otherwise flag itself.
EXEMPT_PATHS = {DENYLIST_MODULE_PATH}

# Paths additionally exempt from the *denylist* check only (still fully
# in scope for the secret-pattern check). LICENSE legitimately, legally
# requires the owner's real name in its copyright line; that is not a
# leak, so it is not a general exemption, just this one check.
DENYLIST_EXEMPT_PATHS = EXEMPT_PATHS | {LICENSE_PATH}


def _load_denylist() -> tuple[str, ...]:
    spec = importlib_util.spec_from_file_location(
        "_public_hygiene_denylist", DENYLIST_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DENYLIST


DENYLIST: tuple[str, ...] = _load_denylist()


def find_secret_matches(text: str) -> list[str]:
    """Return the pattern strings (not the matched substrings themselves,
    to keep offender output free of the secret material) that fire
    against `text`.
    """
    return [pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)]


def find_denylist_matches(text: str, terms: tuple[str, ...]) -> list[str]:
    """Return the terms from `terms` that appear in `text`, case-insensitively
    (a differently-cased former tracker host or the owner's name typed in
    all-caps is exactly the kind of reappearance this is meant to catch).
    """
    lowered = text.lower()
    return [term for term in terms if term and term.lower() in lowered]


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return [
        REPO_ROOT / entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\x00")
        if entry
    ]


def load_file(path: Path) -> str | None:
    """Return the file's text, or None if it is legitimately binary or not
    valid UTF-8 text (both are skipped, not failed). A tracked path that
    cannot be read at all (OSError — missing, permissions, ...) is *not*
    swallowed here: git believes it is tracked, so failing to read it is
    itself a finding, and the caller is responsible for reporting it
    rather than silently treating it as "nothing to see".
    """
    raw = path.read_bytes()  # OSError propagates on purpose; see docstring
    if b"\x00" in raw:
        return None  # binary file, skip
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None  # not valid utf-8 text, skip


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)  # e.g. a planted file outside REPO_ROOT in a test


def scan(
    matcher,
    files: list[Path] | None = None,
    exempt: set[Path] | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Walk `files` (defaults to every tracked file) and apply
    `matcher(text) -> list[str]` to the contents of each one not in
    `exempt` (defaults to EXEMPT_PATHS). Returns (files_scanned,
    offenders), where files_scanned counts only files that were actually
    read as text (not binary/skipped/exempt) — a floor assertion on that
    count is what proves this walk is doing something, independent of
    whether any offenders turn up. `files`/`exempt` are parameters (not
    hardcoded) precisely so a test can plant a real file on disk and run
    it through this exact function, proving the read-then-match pipeline
    end to end rather than only proving its two halves separately.
    """
    if files is None:
        files = tracked_files()
    if exempt is None:
        exempt = EXEMPT_PATHS
    offenders: list[tuple[str, str]] = []
    files_scanned = 0
    for path in files:
        resolved = path.resolve()
        if resolved in exempt:
            continue
        try:
            text = load_file(path)
        except OSError as exc:
            offenders.append((_display_path(path), f"tracked but could not be read: {exc}"))
            continue
        if text is None:
            continue
        files_scanned += 1
        for hit in matcher(text):
            offenders.append((_display_path(path), hit))
    return files_scanned, offenders


def test_the_walk_actually_scans_the_tracked_tree():
    """A positive control on tracked_files()/load_file() themselves: if
    `git ls-files` returned nothing, or every file were (wrongly) treated
    as binary, the sweep tests below would report a false-clean pass. This
    proves the walk is live before trusting its silence.
    """
    files = tracked_files()
    assert REPO_ROOT / "pyproject.toml" in files
    files_scanned, _ = scan(find_secret_matches)
    assert files_scanned > 10, (
        f"expected the sweep to have read more than 10 tracked text files, "
        f"got {files_scanned} — the walk may be silently skipping everything"
    )


def test_no_tracked_file_contains_a_secret_shaped_string():
    files_scanned, offenders = scan(find_secret_matches)
    assert files_scanned > 0
    assert offenders == [], "public-hygiene sweep found secret-shaped strings:\n" + "\n".join(
        f"  {path}: matches {pattern!r}" for path, pattern in offenders
    )


def test_no_tracked_file_contains_a_denylisted_term():
    if any(term.startswith("TODO-") for term in DENYLIST):
        pytest.skip(
            "DENYLIST in tests/_public_hygiene_denylist.py still holds "
            "placeholder TODO- values; the owner/reviewer must supply the "
            "real former-tracker product name and its API host before "
            "this check is meaningful — see that module's docstring. "
            "This is a loud skip, not a pass: it must not be read as "
            "'the denylist was checked and is clean'."
        )
    files_scanned, offenders = scan(
        lambda text: find_denylist_matches(text, DENYLIST),
        exempt=DENYLIST_EXEMPT_PATHS,
    )
    assert files_scanned > 0
    assert offenders == [], "public-hygiene sweep found denylisted terms:\n" + "\n".join(
        f"  {path}: contains {term!r}" for path, term in offenders
    )


def test_the_populated_denylist_entry_is_clean_against_the_real_tree_today():
    """test_no_tracked_file_contains_a_denylisted_term above skips while
    the two TODO- placeholders remain, so it never actually exercises the
    one entry that is already real (the owner's name, sourced from
    LICENSE — see tests/_public_hygiene_denylist.py, the one place this
    file is allowed to spell it out).
    This runs that real entry — not a synthetic stand-in — against the
    full tracked tree with only LICENSE exempted, independent of the
    other two placeholders. It proves today, with real evidence rather
    than a design promise, that populating the owner's name and exempting
    LICENSE for it does not trap the owner into a red suite once the
    remaining two entries are filled in.
    """
    owner_name = next(term for term in DENYLIST if not term.startswith("TODO-"))
    files_scanned, offenders = scan(
        lambda text: find_denylist_matches(text, (owner_name,)),
        exempt=DENYLIST_EXEMPT_PATHS,
    )
    assert files_scanned > 10
    assert offenders == []


class TestMatchersHaveTeeth:
    """Positive controls proving find_secret_matches/find_denylist_matches
    can actually flag something, independent of what is or is not present
    in the tree today. Without these, a typo'd regex or an always-empty
    walk would report the same green result as a genuinely clean repo.
    """

    def test_catches_a_synthetic_aws_style_key(self):
        assert find_secret_matches("AKIA" + "A" * 16)

    def test_catches_a_synthetic_bearer_style_token(self):
        assert find_secret_matches("sk-" + "a" * 25)

    def test_catches_a_pem_private_key_header(self):
        # Built by concatenation, not a single literal: a literal PEM
        # header sitting whole in this file's own source would trip the
        # real sweep against this very file, since only the denylist
        # module (not this test file) is exempt from secret-pattern
        # scanning. See the module docstring.
        header = "-----BEGIN" + " RSA PRIVATE KEY-----"
        assert find_secret_matches(header)

    def test_is_silent_on_ordinary_prose(self):
        assert find_secret_matches("just some ordinary prose, nothing to see") == []

    def test_denylist_matcher_catches_a_present_term(self):
        assert find_denylist_matches(
            "mentions ACME-INTERNAL-TRACKER in passing", ("ACME-INTERNAL-TRACKER",)
        ) == ["ACME-INTERNAL-TRACKER"]

    def test_denylist_matcher_is_case_insensitive(self):
        assert find_denylist_matches(
            "mentions acme-internal-tracker in passing", ("ACME-INTERNAL-TRACKER",)
        ) == ["ACME-INTERNAL-TRACKER"]

    def test_denylist_matcher_is_silent_when_absent(self):
        assert find_denylist_matches("nothing relevant here", ("ACME-INTERNAL-TRACKER",)) == []


class TestScanEndToEnd:
    """scan() itself, run against a real planted file on disk, proving the
    join between "read this path's bytes" and "hand the result to the
    matcher" actually works — a load_file that returned '' or the path
    string for everything would still pass the tests above but would fail
    these.
    """

    def test_finds_a_planted_secret_shaped_string(self, tmp_path):
        planted = tmp_path / "leaked.txt"
        planted.write_text("token=" + "AKIA" + "B" * 16)
        files_scanned, offenders = scan(find_secret_matches, files=[planted])
        assert files_scanned == 1
        assert offenders == [(str(planted), SECRET_PATTERNS[0].pattern)]

    def test_finds_a_planted_denylisted_term(self, tmp_path):
        planted = tmp_path / "leaked.txt"
        planted.write_text("mentions ACME-INTERNAL-TRACKER here")
        files_scanned, offenders = scan(
            lambda text: find_denylist_matches(text, ("ACME-INTERNAL-TRACKER",)),
            files=[planted],
        )
        assert files_scanned == 1
        assert offenders == [(str(planted), "ACME-INTERNAL-TRACKER")]

    def test_is_clean_on_a_planted_file_with_nothing_notable(self, tmp_path):
        planted = tmp_path / "clean.txt"
        planted.write_text("just some ordinary prose, nothing to see")
        files_scanned, offenders = scan(find_secret_matches, files=[planted])
        assert files_scanned == 1
        assert offenders == []

    def test_an_exempt_planted_file_is_skipped_entirely(self, tmp_path):
        planted = tmp_path / "leaked.txt"
        planted.write_text("token=" + "AKIA" + "B" * 16)
        files_scanned, offenders = scan(
            find_secret_matches, files=[planted], exempt={planted.resolve()}
        )
        assert files_scanned == 0
        assert offenders == []
