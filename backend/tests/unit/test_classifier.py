"""The classifier agent, with the scripted fake LLM underneath the real client."""

import pytest

from support_agent.agents.classifier import EXAMPLES_MARKER, Classification, classify, load_prompt
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.errors import StructuredOutputError
from support_agent.llm.fake import FakeProvider, tool_reply

TOOL = "classification"  # the forced tool name for the Classification schema
COMPLAINT = "My order ORD-1001 arrived with a cracked screen."


def make_client(fake: FakeProvider) -> RoutedLLMClient:
    return RoutedLLMClient(
        [fake],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )


async def test_valid_output_is_returned_as_a_classification() -> None:
    args = {
        "category": "damaged_item",
        "confidence": 0.92,
        "order_number": "ORD-1001",
        "reason": "Arrived cracked.",
    }
    fake = FakeProvider([tool_reply(TOOL, args)])

    result = await classify(make_client(fake), COMPLAINT)

    assert result == Classification.model_validate(args)


async def test_customer_text_is_only_ever_the_user_message() -> None:
    fake = FakeProvider(
        [tool_reply(TOOL, {"category": "refund", "confidence": 0.9, "reason": "r"})]
    )

    await classify(make_client(fake), COMPLAINT)

    request = fake.requests[0]
    system, user = request.messages
    assert (system.role, user.role) == ("system", "user")
    assert user.content == COMPLAINT
    assert COMPLAINT not in system.content
    assert request.tier == "small"
    assert request.forced_tool == TOOL


@pytest.mark.parametrize(
    "bad_args",
    [
        {"category": "refund", "confidence": 1.5, "reason": "r"},  # confidence above 1
        {"category": "billing", "confidence": 0.9, "reason": "r"},  # not a category
        {"category": "refund", "confidence": 0.9},  # reason missing
    ],
)
async def test_output_invalid_twice_raises(bad_args: dict[str, object]) -> None:
    # First answer invalid -> one repair attempt -> still invalid -> error.
    fake = FakeProvider([tool_reply(TOOL, bad_args), tool_reply(TOOL, bad_args)])

    with pytest.raises(StructuredOutputError):
        await classify(make_client(fake), COMPLAINT)
    assert len(fake.requests) == 2


def test_few_shot_prompt_has_examples_and_zero_shot_does_not() -> None:
    few_shot = load_prompt(few_shot=True)
    zero_shot = load_prompt(few_shot=False)

    assert "## Examples" in few_shot
    assert "## Examples" not in zero_shot
    assert EXAMPLES_MARKER not in few_shot
    assert "never" in zero_shot and "instructions" in zero_shot  # the injection rule stays
