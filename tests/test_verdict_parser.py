"""
test_verdict_parser.py — offline, dependency-free unit tests for the
verdict-parsing logic used by DeliverableEscrow.verify_deliverable.

Unlike test_deliverable_escrow.py, this file does NOT need `genlayer-test`,
a GenLayer Studio/localnet instance, or live validator consensus. It runs
with plain pytest:

    pytest tests/test_verdict_parser.py

`contracts/deliverable_escrow.py` starts with `from genlayer import *`,
which is only available inside the GenVM contract runtime, so it can't be
imported directly in a normal Python environment. `parse_verdict_response`
itself has no such dependency — it only uses `json`/`re` from the standard
library — so this file extracts just that function (plus the
VALID_VERDICTS constant it uses) straight from the contract source via
`ast`, and execs it into an isolated namespace. That keeps this test
honest: it is exercising the exact code that ships in the contract, not a
hand-copied re-implementation that could quietly drift out of sync.

Coverage focus: the specific regression from the last steward review —
"mixed verdict text that the fallback parser can misread as approval".
The old implementation used an if/elif chain (NEEDS_REVISION, then
APPROVED, then REJECTED) over a plain substring scan, so any response
that happened to contain the word APPROVED anywhere — even while stating
the opposite — was misread as an approval. See test_mixed_verdict_text_*
below for the cases that previously broke.
"""

import ast
import json  # noqa: F401 -- needed in the exec namespace for the loaded code
import re    # noqa: F401 -- needed in the exec namespace for the loaded code
from pathlib import Path

import pytest


def _load_parse_verdict_response():
    """Extract VALID_VERDICTS and parse_verdict_response from the contract
    source without importing the module (which requires the genlayer SDK)."""
    contract_path = (
        Path(__file__).resolve().parent.parent / "contracts" / "deliverable_escrow.py"
    )
    source = contract_path.read_text()
    tree = ast.parse(source, filename=str(contract_path))

    wanted = {"VALID_VERDICTS", "parse_verdict_response"}
    found = {}
    for node in tree.body:
        name = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name
        ):
            name = node.targets[0].id
        if name in wanted:
            found[name] = ast.get_source_segment(source, node)

    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(
            f"Could not locate {missing} in contracts/deliverable_escrow.py — "
            "has the parser been renamed or moved?"
        )

    namespace = {"json": json, "re": re}
    exec(found["VALID_VERDICTS"], namespace)
    exec(found["parse_verdict_response"], namespace)
    return namespace["parse_verdict_response"], namespace["VALID_VERDICTS"]


parse_verdict_response, VALID_VERDICTS = _load_parse_verdict_response()


# ---------------------------------------------------------------------
# Sanity: the clean, expected two-line shape works for all three verdicts
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected_verdict",
    [
        ("APPROVED\nMeets all stated criteria with room to spare.", "APPROVED"),
        ("REJECTED\nDoes not mention validators anywhere in the text.", "REJECTED"),
        ("NEEDS_REVISION\nClose, but missing one required section.", "NEEDS_REVISION"),
        ("NEEDS REVISION\nClose, but missing one required section.", "NEEDS_REVISION"),
    ],
)
def test_clean_two_line_shape(raw, expected_verdict):
    verdict, reasoning = parse_verdict_response(raw)
    assert verdict == expected_verdict
    assert reasoning and reasoning.strip().upper() not in VALID_VERDICTS


def test_json_shape():
    raw = json.dumps(
        {"verdict": "REJECTED", "reasoning": "Only 210 words; needed 300."}
    )
    verdict, reasoning = parse_verdict_response(raw)
    assert verdict == "REJECTED"
    assert reasoning == "Only 210 words; needed 300."


def test_json_shape_in_code_fence():
    raw = '```json\n{"verdict": "APPROVED", "reasoning": "All criteria satisfied."}\n```'
    verdict, reasoning = parse_verdict_response(raw)
    assert verdict == "APPROVED"
    assert reasoning == "All criteria satisfied."


# ---------------------------------------------------------------------
# The regression under test: mixed verdict text must never be silently
# misread as APPROVED.
# ---------------------------------------------------------------------
def test_mixed_verdict_text_rejection_framed_as_not_approved():
    # This is exactly the shape that broke the old elif-chain parser: it
    # contains the word APPROVED, but the actual verdict is a rejection.
    raw = (
        "This submission is not APPROVED. The work is REJECTED because it "
        "never mentions validators as required by the criteria."
    )
    verdict, _ = parse_verdict_response(raw)
    assert verdict != "APPROVED"
    assert verdict == "NEEDS_REVISION"


def test_mixed_verdict_text_quotes_the_criteria():
    # Reasoning that quotes the acceptance criteria back (which itself
    # says "Approve if...") can introduce the word APPROVED even in a
    # response that is actually rejecting the work.
    raw = (
        "REJECTED\n"
        "The criteria says 'Approve if the submission is at least 80 "
        "words and mentions validators' but this submission is only 40 "
        "words, so it does not qualify."
    )
    verdict, _ = parse_verdict_response(raw)
    # First-line signal is unambiguous here, so this should still resolve
    # correctly to REJECTED rather than being pulled off course by the
    # word APPROVED appearing later in the quoted criteria.
    assert verdict == "REJECTED"


def test_mixed_verdict_text_all_three_keywords_present():
    raw = (
        "Considered APPROVED, REJECTED, and NEEDS_REVISION as possible "
        "outcomes before concluding the submission partially satisfies "
        "the brief."
    )
    verdict, _ = parse_verdict_response(raw)
    assert verdict == "NEEDS_REVISION"


def test_mixed_verdict_text_approved_and_rejected_first_line_ambiguous():
    raw = "APPROVED / REJECTED — unclear, defer to human review.\nSee above."
    verdict, _ = parse_verdict_response(raw)
    assert verdict != "APPROVED"
    assert verdict == "NEEDS_REVISION"


# ---------------------------------------------------------------------
# Other edge cases the fallback parser needs to keep handling
# ---------------------------------------------------------------------
def test_bare_verdict_word_only_gets_explicit_no_reasoning_message():
    verdict, reasoning = parse_verdict_response("REJECTED")
    assert verdict == "REJECTED"
    assert "did not return an explanation" in reasoning


def test_empty_response_defaults_to_needs_revision():
    verdict, reasoning = parse_verdict_response("")
    assert verdict == "NEEDS_REVISION"


def test_garbage_response_defaults_to_needs_revision():
    verdict, _ = parse_verdict_response("the validator timed out mid-thought and")
    assert verdict == "NEEDS_REVISION"


def test_verdict_word_with_trailing_punctuation_on_first_line():
    verdict, reasoning = parse_verdict_response(
        "Verdict: APPROVED.\nThe article covers all required points clearly."
    )
    assert verdict == "APPROVED"
    assert "required points" in reasoning
