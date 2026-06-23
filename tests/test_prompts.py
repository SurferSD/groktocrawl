"""Verify the agent system prompts contain expected research-quality sections.

These tests parse the source file directly — no imports, no Docker needed.
Run from repo root:

    python3 -m pytest tests/test_prompts.py -v

Or without pytest:

    python3 tests/test_prompts.py
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESEARCH_PY = REPO / "agent-svc" / "agent" / "research.py"


def extract_prompt(name: str) -> str:
    """Extract a top-level string constant from research.py using the AST."""
    tree = ast.parse(RESEARCH_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == name
                    and isinstance(node.value, ast.Constant)
                ):
                    return node.value.value
    raise ValueError(f"Constant {name} not found in {RESEARCH_PY}")


try:
    SYSTEM_PROMPT = extract_prompt("SYSTEM_PROMPT")
    EXTRACT_SYSTEM_PROMPT = extract_prompt("EXTRACT_SYSTEM_PROMPT")
except Exception as e:
    print(f"ERROR: Could not parse prompts: {e}")
    sys.exit(1)


# --- Tests ---


def test_system_prompt_non_empty():
    assert len(SYSTEM_PROMPT) > 200, (
        f"SYSTEM_PROMPT too short ({len(SYSTEM_PROMPT)} chars)"
    )


def test_system_prompt_has_determined_identity():
    assert "determined" in SYSTEM_PROMPT.lower()


def test_system_prompt_has_source_quality_section():
    assert "SOURCE QUALITY" in SYSTEM_PROMPT


def test_system_prompt_contains_source_ladder():
    assert "high" in SYSTEM_PROMPT.lower() and "low" in SYSTEM_PROMPT.lower()
    assert "authority" in SYSTEM_PROMPT.lower()


def test_system_prompt_has_synthesis_section():
    assert "SYNTHESIS" in SYSTEM_PROMPT


def test_system_prompt_addresses_contradictions():
    assert "contradict" in SYSTEM_PROMPT.lower() or "conflict" in SYSTEM_PROMPT.lower()


def test_system_prompt_looks_for_consensus():
    assert "consensus" in SYSTEM_PROMPT.lower()


def test_system_prompt_has_integrity_section():
    assert "INTEGRITY" in SYSTEM_PROMPT


def test_system_prompt_restricts_to_context():
    assert "ONLY" in SYSTEM_PROMPT


def test_system_prompt_forbids_fabrication():
    assert "fabricate" in SYSTEM_PROMPT.lower()


def test_system_prompt_requires_citations():
    assert "Cite sources" in SYSTEM_PROMPT


def test_system_prompt_has_output_quality_section():
    assert "OUTPUT QUALITY" in SYSTEM_PROMPT


def test_system_prompt_mentions_structured_output():
    assert "JSON" in SYSTEM_PROMPT


def test_system_prompt_no_be_concise():
    """'Be concise' was removed — replaced with thoroughness."""
    assert "be concise" not in SYSTEM_PROMPT.lower()


def test_extract_prompt_non_empty():
    assert len(EXTRACT_SYSTEM_PROMPT) > 150, (
        f"EXTRACT_SYSTEM_PROMPT too short ({len(EXTRACT_SYSTEM_PROMPT)} chars)"
    )


def test_extract_prompt_extract_all():
    assert "ALL" in EXTRACT_SYSTEM_PROMPT


def test_extract_prompt_no_stop_after_first():
    assert "do not stop" in EXTRACT_SYSTEM_PROMPT.lower()


def test_extract_prompt_handles_missing_data():
    assert (
        "missing" in EXTRACT_SYSTEM_PROMPT.lower()
        or "ambiguous" in EXTRACT_SYSTEM_PROMPT.lower()
    )


# --- Main: run without pytest ---
if __name__ == "__main__":
    tests = [
        ("SYSTEM_PROMPT non-empty", test_system_prompt_non_empty),
        (
            "SYSTEM_PROMPT has determined identity",
            test_system_prompt_has_determined_identity,
        ),
        (
            "SYSTEM_PROMPT has SOURCE QUALITY section",
            test_system_prompt_has_source_quality_section,
        ),
        (
            "SYSTEM_PROMPT contains source ladder",
            test_system_prompt_contains_source_ladder,
        ),
        (
            "SYSTEM_PROMPT has SYNTHESIS section",
            test_system_prompt_has_synthesis_section,
        ),
        (
            "SYSTEM_PROMPT addresses contradictions",
            test_system_prompt_addresses_contradictions,
        ),
        ("SYSTEM_PROMPT looks for consensus", test_system_prompt_looks_for_consensus),
        (
            "SYSTEM_PROMPT has INTEGRITY section",
            test_system_prompt_has_integrity_section,
        ),
        ("SYSTEM_PROMPT restricts to context", test_system_prompt_restricts_to_context),
        ("SYSTEM_PROMPT forbids fabrication", test_system_prompt_forbids_fabrication),
        ("SYSTEM_PROMPT requires citations", test_system_prompt_requires_citations),
        (
            "SYSTEM_PROMPT has OUTPUT QUALITY section",
            test_system_prompt_has_output_quality_section,
        ),
        (
            "SYSTEM_PROMPT mentions structured output",
            test_system_prompt_mentions_structured_output,
        ),
        ("SYSTEM_PROMPT no 'be concise'", test_system_prompt_no_be_concise),
        ("EXTRACT_SYSTEM_PROMPT non-empty", test_extract_prompt_non_empty),
        ("EXTRACT_SYSTEM_PROMPT extract ALL", test_extract_prompt_extract_all),
        (
            "EXTRACT_SYSTEM_PROMPT no stop after first",
            test_extract_prompt_no_stop_after_first,
        ),
        (
            "EXTRACT_SYSTEM_PROMPT handles missing/ambiguous",
            test_extract_prompt_handles_missing_data,
        ),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1

    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
