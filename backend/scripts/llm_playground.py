"""Try the real Groq API through our LLM client and see two LLM basics for yourself.

1. Tokens: send one sample complaint and print how many input and output tokens it used.
   Free-tier limits and cost are counted in tokens, not characters.
2. Temperature: ask the same short question 5 times at temperature 0 and 5 times at
   temperature 1. At 0 the model (almost) always picks its most likely next token, so
   the answers repeat. At 1 it samples, so the answers vary.

Uses Groq only (no Gemini fallback), so every answer you see comes from Groq.
Cheap on purpose: 11 calls to the small model, capped at a few dozen output tokens each.
Needs GROQ_API_KEY in .env. Not run in CI or pytest.

    uv run python scripts/llm_playground.py
"""

import asyncio
import logging
import sys

from support_agent.core.config import get_settings
from support_agent.core.logging import configure_logging
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.errors import LLMError
from support_agent.llm.providers import GroqProvider
from support_agent.llm.types import customer_turn

# Synthetic customer text (no real person or order).
SAMPLE_COMPLAINT = (
    "Hi, I ordered a mixer grinder (ORD-1042) and it arrived with a cracked jar. "
    "I have photos. I'd like a refund to my UPI account, not a replacement. "
    "This is the second time this happened, please sort it out quickly."
)

SHORT_QUESTION = "Suggest one name for a friendly customer support bot. Reply with the name only."
RUNS = 5


def build_groq_client() -> RoutedLLMClient:
    settings = get_settings()
    if settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in .env. Add it and run again.")
    groq = GroqProvider(
        settings.groq_api_key,  # passed straight to the SDK; never printed
        settings.groq_small_model,
        settings.groq_large_model,
        settings.llm_timeout_seconds,
        settings.groq_reasoning_effort,
    )
    retry = RetrySettings(
        max_attempts=settings.llm_max_attempts,
        backoff_base_seconds=settings.llm_backoff_base_seconds,
        backoff_max_seconds=settings.llm_backoff_max_seconds,
    )
    return RoutedLLMClient(
        [groq],
        retry,
        default_temperature=settings.llm_temperature,
        default_max_tokens=settings.llm_max_tokens,
    )


async def show_token_counts(client: RoutedLLMClient) -> None:
    print("=== 1. Token counts for a sample complaint ===")
    print(f"Complaint ({len(SAMPLE_COMPLAINT)} characters):\n  {SAMPLE_COMPLAINT}\n")
    messages = customer_turn(
        "You are a support assistant. Summarise the customer's problem in one sentence.",
        SAMPLE_COMPLAINT,
    )
    result = await client.chat(messages, max_tokens=150)
    usage = result.usage
    print(f"Model:         {result.model}")
    print(f"Summary:       {result.content.strip()}")
    print(f"Input tokens:  {usage.input_tokens}  (system prompt + complaint + chat formatting)")
    hidden = usage.reasoning_tokens  # already included in output_tokens
    print(f"Output tokens: {usage.output_tokens}  ({hidden} of them hidden reasoning)\n")


async def show_temperature(client: RoutedLLMClient) -> None:
    print("=== 2. Same prompt, different temperatures ===")
    print(f"Prompt: {SHORT_QUESTION}\n")
    messages = customer_turn("You are a helpful assistant.", SHORT_QUESTION)
    for temperature in (0.0, 1.0):
        print(f"Temperature {temperature}:")
        answers = []
        for run in range(1, RUNS + 1):
            # Room for a little reasoning plus a one-word answer.
            result = await client.chat(messages, temperature=temperature, max_tokens=60)
            answers.append(result.content.strip())
            print(f"  run {run}: {answers[-1]}")
        print(f"  -> {len(set(answers))} different answer(s) out of {RUNS}\n")


async def main() -> None:
    # Show the usage log lines (JSON) between the results: numbers only, no text or key.
    configure_logging("INFO")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # hide its per-request lines
    client = build_groq_client()
    try:
        await show_token_counts(client)
        await show_temperature(client)
    except LLMError as exc:
        # Our error messages never contain prompts, responses or keys.
        sys.exit(f"LLM call failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
