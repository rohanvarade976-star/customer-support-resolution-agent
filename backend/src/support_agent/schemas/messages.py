"""The customer's reply to a run that asked them something (POST /runs/{run_id}/messages).

The text is untrusted data: it reaches the agents only inside `wrap_untrusted` markers, and
it is never written to audit events or logs.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

MAX_MESSAGE_LENGTH = 2000

MessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH)
]


class CustomerMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: MessageText
