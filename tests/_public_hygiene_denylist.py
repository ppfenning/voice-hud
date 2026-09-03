"""Literal denylist constant for tests/test_public_hygiene.py.

Split into its own module so the public-hygiene sweep can exclude *only*
this constant from scanning — per the ticket's "skip ... this test file's
own denylist constant" instruction — while test_public_hygiene.py's own
body (including its SECRET_PATTERNS and its logic) stays fully in scope
for the sweep it defines. This is a plain Python module, not a config
file: the initiative's instruction that the denylist stay literal Python
strings rather than move into a YAML/JSON config is still honored, it is
just factored one file over so the exemption in test_public_hygiene.py
does not have to swallow the whole test file to avoid self-matching.

Corrected premise (a prior version of this docstring claimed none of the
three entries were recoverable from the sanitized tree — that was wrong
for the third one, see below):

- The former tracker's product name and its API host genuinely are not
  recoverable from this tree. They stay TODO placeholders until the
  owner/reviewer supplies the real literals from firsthand knowledge; do
  not guess plausible-looking real values for either.
- The repo owner's real name is *not* unrecoverable: it is sitting,
  legitimately, in this tracked repo's own LICENSE file (`Copyright (c)
  2026 Patrick Pfenning`) — an MIT license's copyright line requires the
  holder's real name, so that occurrence is not a leak. It is populated
  below for real, evidenced by that file rather than guessed. Because it
  is legitimately required in LICENSE, test_public_hygiene.py exempts
  LICENSE specifically from the denylist check (not from the
  secret-pattern check, which does not fire on it anyway) — see
  DENYLIST_EXEMPT_PATHS there.

test_no_tracked_file_contains_a_denylisted_term still skips loudly (it
does not report a false pass) as long as either of the first two entries
is still a TODO- placeholder — see
test_public_hygiene.py:test_no_tracked_file_contains_a_denylisted_term.
"""

from __future__ import annotations

# TODO(owner/reviewer): replace with the real literal strings from
# firsthand knowledge of the pre-sanitization repo. Do not guess
# plausible-looking real values here.
DENYLIST: tuple[str, ...] = (
    "TODO-FORMER-TRACKER-PRODUCT-NAME",
    "TODO-FORMER-TRACKER-API-HOST",
    "Patrick Pfenning",  # from LICENSE:3; see module docstring
)
