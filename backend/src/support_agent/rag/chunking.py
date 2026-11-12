"""Split a policy into one chunk per numbered section.

A policy body looks like this:

    # Refund Policy (v2)          <- optional title line, ignored (the title is in front matter)
    ## 1. Purpose and scope       <- section 1
    text...
    ## 2. Who can request a refund
    text...

Each section becomes one chunk with a stable id such as "refund_policy_v2:section_3".
That id is what agents cite, so it must not change when the text of a section changes.
Our sections are short, so a section is never split further.
"""

import re

from pydantic import BaseModel, ConfigDict

from support_agent.rag.documents import PolicyFile, PolicyFormatError

SECTION_HEADING = re.compile(r"^## (\d+)\. (\S.*)$")  # "## 3. Return window"


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_key: str  # "refund_policy_v2:section_3", unique across all policies
    section: str  # "section_3"
    section_number: int
    section_title: str  # "Return window"
    content: str  # the section text, without its heading
    # What gets embedded: the titles help a search for "refund window" find this section
    # even if the body never repeats those words.
    embed_text: str


def chunk_policy(policy: PolicyFile) -> list[Chunk]:
    """Return the sections of a policy in order. Raises PolicyFormatError on bad structure."""
    sections: list[tuple[int, str, list[str]]] = []  # (number, title, body lines)
    for line in policy.body.split("\n"):
        heading = SECTION_HEADING.match(line)
        if heading:
            sections.append((int(heading.group(1)), heading.group(2).strip(), []))
        elif line.startswith("## "):
            raise PolicyFormatError(f"{policy.source_uri}: heading must be '## N. Title': {line}")
        elif sections:
            sections[-1][2].append(line)
        elif line.strip() and not line.startswith("# "):
            # Text before section 1 would belong to no chunk and never be searchable.
            raise PolicyFormatError(f"{policy.source_uri}: text before the first section")

    if not sections:
        raise PolicyFormatError(f"{policy.source_uri}: no '## N. Title' sections found")
    numbers = [number for number, _, _ in sections]
    if numbers != list(range(1, len(sections) + 1)):
        raise PolicyFormatError(f"{policy.source_uri}: sections must be numbered 1, 2, 3, ...")

    meta = policy.metadata
    chunks: list[Chunk] = []
    for number, title, body_lines in sections:
        content = "\n".join(body_lines).strip()
        if not content:
            raise PolicyFormatError(f"{policy.source_uri}: section {number} is empty")
        section = f"section_{number}"
        chunks.append(
            Chunk(
                chunk_key=f"{policy.document_key}:{section}",
                section=section,
                section_number=number,
                section_title=title,
                content=content,
                embed_text=f"{meta.title} {meta.version}, section {number}: {title}\n\n{content}",
            )
        )
    return chunks
