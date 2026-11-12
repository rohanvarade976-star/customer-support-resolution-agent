from datetime import date
from pathlib import Path

import pytest

from support_agent.rag.documents import (
    PolicyFile,
    PolicyFormatError,
    load_policy_directory,
    load_policy_file,
    parse_policy,
    validate_policy_set,
)

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

BODY = "# Example\n\n## 1. Scope\n\nSome text.\n"


def policy_text(**overrides: object) -> str:
    """A valid shipping policy file; pass key=value to change a field, key=None to drop it."""
    fields: dict[str, object] = {
        "policy_key": "shipping_delays",
        "version": "v1",
        "title": "Shipping Delays Policy",
        "effective_from": "2025-01-01",
        "effective_to": "null",
    }
    fields.update(overrides)
    lines = [f"{key}: {value}" for key, value in fields.items() if value is not None]
    return "---\n" + "\n".join(lines) + "\n---\n" + BODY


def test_valid_front_matter_is_parsed() -> None:
    policy = parse_policy(policy_text(), "shipping_delays_v1.md")

    assert policy.metadata.policy_key == "shipping_delays"
    assert policy.metadata.effective_from == date(2025, 1, 1)
    assert policy.metadata.effective_to is None
    assert policy.document_key == "shipping_delays_v1"
    assert policy.body.startswith("# Example")


def test_windows_line_endings_give_the_same_result() -> None:
    text = policy_text()
    assert parse_policy(text.replace("\n", "\r\n"), "x.md") == parse_policy(text, "x.md")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"title": None}, "title"),
        ({"effective_to": None}, "effective_to"),  # the key must be present, even as null
        ({"version": "2"}, "version"),
        ({"effective_to": "2024-12-31"}, "effective_to must be after effective_from"),
        ({"refund_window_days": 30}, "only refund_policy"),
        ({"policy_key": "refund_policy"}, "refund_policy needs refund_window_days"),
        ({"owner": "someone"}, "owner"),  # unknown keys are rejected
    ],
)
def test_invalid_front_matter_is_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(PolicyFormatError, match=message):
        parse_policy(policy_text(**overrides), "bad.md")


def test_missing_front_matter_is_rejected() -> None:
    with pytest.raises(PolicyFormatError, match="must start with"):
        parse_policy(BODY, "bad.md")
    with pytest.raises(PolicyFormatError, match="no closing"):
        parse_policy("---\npolicy_key: x\n" + BODY, "bad.md")


def test_file_name_must_match_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "shipping_delays_v2.md"
    path.write_text(policy_text(), encoding="utf-8")

    with pytest.raises(PolicyFormatError, match=r"shipping_delays_v1.md"):
        load_policy_file(path)


def _version(version: str, start: str, end: str) -> PolicyFile:
    return parse_policy(
        policy_text(version=version, effective_from=start, effective_to=end), f"{version}.md"
    )


def test_overlapping_versions_are_rejected() -> None:
    validate_policy_set(
        [_version("v1", "2025-01-01", "2025-06-30"), _version("v2", "2025-07-01", "null")]
    )

    with pytest.raises(PolicyFormatError, match="overlapping"):
        validate_policy_set(
            [_version("v1", "2025-01-01", "2025-07-01"), _version("v2", "2025-07-01", "null")]
        )
    with pytest.raises(PolicyFormatError, match="overlapping"):  # v1 never ends
        validate_policy_set(
            [_version("v1", "2025-01-01", "null"), _version("v2", "2025-07-01", "null")]
        )


def test_duplicate_versions_are_rejected() -> None:
    policy = parse_policy(policy_text(), "a.md")
    with pytest.raises(PolicyFormatError, match="duplicate"):
        validate_policy_set([policy, policy])


def test_the_real_policy_folder_is_valid() -> None:
    policies = {p.document_key: p.metadata for p in load_policy_directory(POLICY_DIR)}

    assert set(policies) == {
        "refund_policy_v1",
        "refund_policy_v2",
        "return_processing_v1",
        "damaged_products_v1",
        "shipping_delays_v1",
        "escalation_exceptions_v1",
    }
    v1, v2 = policies["refund_policy_v1"], policies["refund_policy_v2"]
    assert v1.refund_window_days == 15
    assert v1.effective_to == date(2026, 6, 30)
    assert v2.effective_from == date(2026, 7, 1)  # starts the day after v1 ends
    assert v2.effective_to is None
