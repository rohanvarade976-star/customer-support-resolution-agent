"""The policy text and the code must agree. If one changes, this test makes you change both."""

from pathlib import Path

from support_agent.core.config import get_settings
from support_agent.rag.documents import load_policy_file

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"


def test_refund_policy_v2_window_matches_settings() -> None:
    policy = load_policy_file(POLICY_DIR / "refund_policy_v2.md")

    assert policy.metadata.refund_window_days == get_settings().refund_window_days
    # The prose must say the same number as the front matter.
    assert f"{policy.metadata.refund_window_days} days to request a return" in policy.body
