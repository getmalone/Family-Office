"""
Shared state definition for the Family Office LangGraph agents.

All agents share this state via the supervisor graph, enabling
collaborative workflows like tax-loss harvesting cycles that span
multiple agents (Tax -> Portfolio -> Accounting -> Reporting).
"""

from typing import Annotated, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class FamilyOfficeState(TypedDict):
    """
    Shared state for the Family Office multi-agent supervisor graph.

    The state flows between agents as the supervisor routes requests.
    Messages accumulate via add_messages annotation. The context dict
    carries session-scoped data (selected entity, date range, etc.).
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]
    pending_approvals: list[dict]
    current_agent: str | None
    context: dict
