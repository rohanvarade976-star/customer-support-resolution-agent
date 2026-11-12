"""Policy documents: Markdown files that start with a YAML front matter block.

    ---
    policy_key: refund_policy
    version: v2
    title: Refund Policy
    effective_from: 2026-07-01
    effective_to: null          # null = still in force; otherwise the last day it applies
    refund_window_days: 30      # refund_policy only
    ---
    # Refund Policy (v2)
    ## 1. Purpose and scope
    ...

This module only reads and checks files. It never touches the database.
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

FRONT_MATTER_DELIMITER = "---"
REFUND_POLICY_KEY = "refund_policy"


class PolicyFormatError(ValueError):
    """A policy file, or the set of policy files, breaks a rule."""


class PolicyMetadata(BaseModel):
    """The front matter. Every key is required except refund_window_days."""

    model_config = ConfigDict(frozen=True, extra="forbid")  # a typo'd key is an error

    policy_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    version: str = Field(pattern=r"^v[1-9][0-9]*$")  # v1, v2, ...
    title: str = Field(min_length=1, max_length=200)
    effective_from: date
    # No default: the key must be written, even if its value is null. Inclusive last day.
    effective_to: date | None
    refund_window_days: int | None = Field(default=None, ge=1, le=365)

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        # Same rule as the CHECK constraint on policy_documents.
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        is_refund_policy = self.policy_key == REFUND_POLICY_KEY
        if is_refund_policy and self.refund_window_days is None:
            raise ValueError("refund_policy needs refund_window_days")
        if not is_refund_policy and self.refund_window_days is not None:
            raise ValueError("only refund_policy may set refund_window_days")
        return self


class PolicyFile(BaseModel):
    """One parsed policy file: its checked front matter and its Markdown body."""

    model_config = ConfigDict(frozen=True)

    metadata: PolicyMetadata
    body: str
    source_uri: str  # the file name, e.g. "refund_policy_v2.md"

    @property
    def document_key(self) -> str:
        """Unique name of this version, e.g. "refund_policy_v2". Prefix of every chunk id."""
        return f"{self.metadata.policy_key}_{self.metadata.version}"


def parse_policy(text: str, source_uri: str) -> PolicyFile:
    """Split the front matter from the body and validate it."""
    # Windows checkouts may use CRLF. Normalise so the same file always hashes the same.
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise PolicyFormatError(f"{source_uri}: must start with a '---' front matter line")
    try:
        end = next(
            i for i, line in enumerate(lines[1:], start=1) if line.strip() == FRONT_MATTER_DELIMITER
        )
    except StopIteration:
        raise PolicyFormatError(f"{source_uri}: front matter has no closing '---'") from None

    # safe_load builds only plain data (dicts, strings, numbers, dates), never objects.
    raw = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(raw, dict):
        raise PolicyFormatError(f"{source_uri}: front matter must be a set of 'key: value' lines")
    try:
        metadata = PolicyMetadata.model_validate(raw)
    except ValidationError as error:
        raise PolicyFormatError(f"{source_uri}: invalid front matter: {error}") from error

    body = "\n".join(lines[end + 1 :]).strip()
    return PolicyFile(metadata=metadata, body=body, source_uri=source_uri)


def load_policy_file(path: Path) -> PolicyFile:
    policy = parse_policy(path.read_text(encoding="utf-8"), source_uri=path.name)
    # The file name and the front matter must agree, so a copied file cannot
    # silently overwrite another version.
    if path.stem != policy.document_key:
        raise PolicyFormatError(f"{path.name}: file name must be '{policy.document_key}.md'")
    return policy


def validate_policy_set(policies: Sequence[PolicyFile]) -> None:
    """Rules that involve several files: no duplicate versions, no overlapping dates."""
    duplicates = [key for key, n in Counter(p.document_key for p in policies).items() if n > 1]
    if duplicates:
        raise PolicyFormatError(f"duplicate policy versions: {', '.join(sorted(duplicates))}")

    versions: defaultdict[str, list[PolicyMetadata]] = defaultdict(list)
    for policy in policies:
        versions[policy.metadata.policy_key].append(policy.metadata)
    for policy_key, metas in versions.items():
        metas.sort(key=lambda meta: meta.effective_from)
        for earlier, later in pairwise(metas):
            # effective_to is the last day, so the next version must start after it.
            if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
                raise PolicyFormatError(
                    f"{policy_key} {earlier.version} and {later.version} have overlapping dates"
                )


def load_policy_directory(directory: Path) -> list[PolicyFile]:
    """Load and check every *.md file in the directory, sorted by file name."""
    policies = [load_policy_file(path) for path in sorted(directory.glob("*.md"))]
    if not policies:
        raise PolicyFormatError(f"no policy files (*.md) found in {directory}")
    validate_policy_set(policies)
    return policies
