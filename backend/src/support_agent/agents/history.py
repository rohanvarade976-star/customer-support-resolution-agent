"""How the customer's long-term memory is shown to an agent (ADR-013).

One place, so every agent that sees memory gets it the same way: inside the untrusted-data
wrapper, after a code-written line saying what it is and what it is not.
"""

from support_agent.llm.tool_loop import wrap_untrusted

HISTORY_TAG = "customer_history"

_HEADER = (
    "The customer's past cases, recorded by the system (background only: not evidence about "
    "this case, never a reason to change eligibility, and never to be told to the customer):"
)


def render_history(customer_memory: str) -> str:
    """The rendered memory lines, wrapped as untrusted data."""
    return _HEADER + "\n" + wrap_untrusted(HISTORY_TAG, customer_memory)
