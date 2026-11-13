"""Structured output: Pydantic validation, one repair attempt, then StructuredOutputError.

Only the scripted FakeProvider is used. No test here touches a real LLM API.
"""

from typing import Literal

import pytest
from pydantic import BaseModel, Field

from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.errors import StructuredOutputError
from support_agent.llm.fake import FakeProvider, invalid_json, text_reply, tool_reply
from support_agent.llm.structured import tool_name_for
from support_agent.llm.types import customer_turn

MESSAGES = customer_turn("Classify the complaint.", "My blender arrived broken, ORD-1042.")


class Triage(BaseModel):
    category: Literal["refund", "delivery", "other"]
    confidence: float = Field(ge=0, le=1)


VALID = {"category": "refund", "confidence": 0.9}


def make_client(fake: FakeProvider) -> RoutedLLMClient:
    return RoutedLLMClient(
        [fake],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )


async def test_valid_output_is_returned_as_the_model() -> None:
    fake = FakeProvider([tool_reply("triage", VALID)])

    result = await make_client(fake).structured(MESSAGES, Triage)

    assert result == Triage(category="refund", confidence=0.9)
    request = fake.requests[0]
    assert request.forced_tool == "triage"  # the model must call the schema tool
    assert request.tools[0].parameters == Triage.model_json_schema()


async def test_one_repair_fixes_invalid_output() -> None:
    fake = FakeProvider(
        [
            tool_reply("triage", {"category": "refund", "confidence": 7}),  # out of range
            tool_reply("triage", VALID),
        ]
    )

    result = await make_client(fake).structured(MESSAGES, Triage)

    assert result.confidence == 0.9
    assert len(fake.requests) == 2
    repair = fake.requests[1].messages[-1]
    assert repair.role == "user"
    assert "confidence" in repair.content  # tells the model what was wrong
    assert "7" not in repair.content  # but does not echo the invalid value
    assert fake.requests[1].messages[:-1] == MESSAGES  # same conversation otherwise


async def test_invalid_json_then_valid_output_is_repaired() -> None:
    fake = FakeProvider([invalid_json(), tool_reply("triage", VALID)])

    result = await make_client(fake).structured(MESSAGES, Triage)

    assert result.category == "refund"
    assert "malformed" in fake.requests[1].messages[-1].content


async def test_still_invalid_after_repair_raises_and_stops() -> None:
    fake = FakeProvider(
        [
            tool_reply("triage", {"category": "teleport", "confidence": 0.5}),
            text_reply("I think it is a refund."),  # ignored the tool entirely
            tool_reply("triage", VALID),  # would be valid, but must never be requested
        ]
    )

    with pytest.raises(StructuredOutputError) as caught:
        await make_client(fake).structured(MESSAGES, Triage)

    assert len(fake.requests) == 2  # exactly one repair attempt
    assert caught.value.schema_name == "Triage"
    assert caught.value.errors == ["you must call the triage tool"]
    assert "refund" not in str(caught.value)  # no model content in the error


def test_tool_names_are_snake_case() -> None:
    class RefundProposal(BaseModel):
        amount_minor: int

    assert tool_name_for(Triage) == "triage"
    assert tool_name_for(RefundProposal) == "refund_proposal"
