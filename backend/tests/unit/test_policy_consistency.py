"""Each refund policy's prose must state the same window as its own front matter.

Eligibility uses the front matter (`refund_window_days`, ingested into the database) of the
version in force on the order's date (ADR-005). Agents cite the prose. If the two ever
disagreed, the cited policy and the decision would contradict each other.
"""

from pathlib import Path

import pytest

from support_agent.rag.documents import load_policy_file

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"
REFUND_POLICY_FILES = sorted(POLICY_DIR.glob("refund_policy_*.md"))


def test_both_refund_policy_versions_are_checked() -> None:
    assert [path.name for path in REFUND_POLICY_FILES] == [
        "refund_policy_v1.md",
        "refund_policy_v2.md",
    ]


@pytest.mark.parametrize("path", REFUND_POLICY_FILES, ids=lambda path: path.stem)
def test_prose_window_matches_front_matter(path: Path) -> None:
    policy = load_policy_file(path)

    days = policy.metadata.refund_window_days
    assert days is not None
    assert f"{days} days to request a return" in policy.body
