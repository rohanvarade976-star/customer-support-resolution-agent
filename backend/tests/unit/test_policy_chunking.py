from pathlib import Path

import pytest

from support_agent.rag.chunking import Chunk, chunk_policy
from support_agent.rag.documents import PolicyFormatError, load_policy_directory, parse_policy

POLICY_DIR = Path(__file__).resolve().parents[2] / "data" / "policies"

FRONT_MATTER = """---
policy_key: refund_policy
version: v2
title: Refund Policy
effective_from: 2026-07-01
effective_to: null
refund_window_days: 30
---
"""


def chunks_for(body: str) -> list[Chunk]:
    return chunk_policy(parse_policy(FRONT_MATTER + body, "refund_policy_v2.md"))


def test_one_chunk_per_section_with_stable_ids_and_titles() -> None:
    chunks = chunks_for(
        "# Refund Policy (v2)\n\n## 1. Scope\n\nWho it covers.\n\n"
        "## 2. Return window\n\nThirty days.\n\nDay 30 counts.\n"
    )

    assert [c.chunk_key for c in chunks] == [
        "refund_policy_v2:section_1",
        "refund_policy_v2:section_2",
    ]
    assert [c.section for c in chunks] == ["section_1", "section_2"]
    assert [c.section_title for c in chunks] == ["Scope", "Return window"]
    assert chunks[1].content == "Thirty days.\n\nDay 30 counts."  # heading not repeated


def test_embed_text_includes_document_and_section_titles() -> None:
    chunk = chunks_for("## 1. Return window\n\nThirty days.\n")[0]

    assert chunk.embed_text == "Refund Policy v2, section 1: Return window\n\nThirty days."


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("## 1. A\n\ntext\n\n## 3. C\n\ntext\n", "numbered 1, 2, 3"),
        ("## 2. A\n\ntext\n", "numbered 1, 2, 3"),
        ("## Return window\n\ntext\n", "must be '## N. Title'"),
        ("Loose text.\n\n## 1. A\n\ntext\n", "text before the first section"),
        ("## 1. A\n\n## 2. B\n\ntext\n", "section 1 is empty"),
        ("# Only a title\n", "no '## N. Title' sections"),
    ],
)
def test_bad_structure_is_rejected(body: str, message: str) -> None:
    with pytest.raises(PolicyFormatError, match=message):
        chunks_for(body)


def test_every_real_policy_chunks_cleanly_with_unique_ids() -> None:
    keys = [c.chunk_key for p in load_policy_directory(POLICY_DIR) for c in chunk_policy(p)]

    assert len(keys) == len(set(keys))
    assert "refund_policy_v2:section_3" in keys  # the PRD's example citation exists
