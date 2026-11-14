"""Plain `uuid.UUID` values at the edge of the workflow (ADR-017).

asyncpg returns its own `UUID` subclass for uuid columns. LangGraph's checkpoint serializer
writes a value under its class path, and reads it back only if that class is allowed. The
driver's class is not ours to allow (it would widen the allowlist to a third-party module),
so ids are converted to the standard class before they enter the workflow state.
"""

from uuid import UUID


def std_uuid(value: UUID) -> UUID:
    """The same id as an exact `uuid.UUID` (a subclass instance is rebuilt from its integer)."""
    return value if type(value) is UUID else UUID(int=value.int)
