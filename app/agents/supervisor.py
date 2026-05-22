"""
Supervisor agent for the Family Office multi-agent system.

Routes user requests to the appropriate specialist agent using LangGraph's
supervisor pattern. Handles human-in-the-loop approval workflows.

The Family Office is a dedicated private investment and wealth management entity
established exclusively for the benefit of the Principal (the user), their spouse,
and their children (current and future).
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy.orm import Session, sessionmaker

from app.agents.portfolio_agent import PORTFOLIO_AGENT_PROMPT
from app.agents.tax_agent import TAX_AGENT_PROMPT
from app.agents.accounting_agent import ACCOUNTING_AGENT_PROMPT
from app.agents.reporting_agent import REPORTING_AGENT_PROMPT
from app.agents.estate_agent import ESTATE_AGENT_PROMPT
from app.agents.analysis_agent import ANALYSIS_AGENT_PROMPT

from app.agents.tools.portfolio_tools import create_portfolio_tools
from app.agents.tools.tax_tools import create_tax_tools
from app.agents.tools.accounting_tools import create_accounting_tools
from app.agents.tools.reporting_tools import create_reporting_tools
from app.agents.tools.estate_tools import create_estate_tools
from app.agents.tools.approval_tools import create_approval_tools
from app.agents.tools.analysis_tools import create_analysis_tools


SUPERVISOR_PROMPT = """You are the Supervisor for the Family Office agentic AI system.

The Family Office is a dedicated private investment and wealth management entity
established exclusively for the benefit of the Principal, their spouse, and their
children (current and future). It operates as a tax-efficient investment vehicle.

You coordinate six specialist agents:

1. **portfolio_agent** — Portfolio monitoring, trade execution, cost basis tracking
   Route here for: positions, holdings, trades, market data, allocation, rebalancing

2. **tax_agent** — Tax-loss harvesting, election simulation, expense tracking
   Route here for: tax optimization, harvesting, wash sales, tax summary, elections

3. **accounting_agent** — Double-entry bookkeeping, financial statements
   Route here for: journal entries, trial balance, income statement, balance sheet

4. **reporting_agent** — Reports, compliance, tax exports
   Route here for: quarterly reports, Schedule D, Form 8949, compliance flags

5. **estate_agent** — Ownership tracking, gifting, GRAT simulation
   Route here for: gifts, estate planning, GRATs, SLATs, ownership, beneficiaries

6. **analysis_agent** — Risk analysis, investment profiles, rebalancing, Monte Carlo
   Route here for: portfolio risk, volatility, Sharpe ratio, drawdown, beta, target allocation,
   rebalancing, drift, Monte Carlo, financial projections, investment profiles, conservative,
   moderate, balanced, growth, aggressive

Route the user's request to the most appropriate agent. If a request spans multiple
domains, start with the most relevant agent — they can collaborate via you.

For general questions about the Family Office, provide direct answers using your
knowledge of the system.
"""


def build_supervisor(
    session_factory: sessionmaker[Session],
    llm=None,
):
    """
    Build the LangGraph supervisor graph with all 5 specialist agents.

    Returns a compiled graph ready for invocation with human-in-the-loop
    interrupt support for material decisions.
    """
    try:
        from langgraph.prebuilt import create_react_agent
        from langgraph_supervisor import create_supervisor
    except ImportError:
        # Return a simple fallback if LangGraph is not available
        return _create_fallback_supervisor(session_factory)

    if llm is None:
        llm = _get_default_llm()
        if llm is None:
            return _create_fallback_supervisor(session_factory)

    @contextmanager
    def session_ctx() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # Create tools for each agent
    approval_tools = create_approval_tools(session_ctx)
    portfolio_tools = create_portfolio_tools(session_ctx) + approval_tools
    tax_tools = create_tax_tools(session_ctx) + approval_tools
    accounting_tools = create_accounting_tools(session_ctx)
    reporting_tools = create_reporting_tools(session_ctx)
    estate_tools = create_estate_tools(session_ctx) + approval_tools
    analysis_tools = create_analysis_tools(session_ctx)

    # Create specialist agents
    portfolio_agent = create_react_agent(
        model=llm,
        tools=portfolio_tools,
        name="portfolio_agent",
        prompt=PORTFOLIO_AGENT_PROMPT,
    )

    tax_agent = create_react_agent(
        model=llm,
        tools=tax_tools,
        name="tax_agent",
        prompt=TAX_AGENT_PROMPT,
    )

    accounting_agent = create_react_agent(
        model=llm,
        tools=accounting_tools,
        name="accounting_agent",
        prompt=ACCOUNTING_AGENT_PROMPT,
    )

    reporting_agent = create_react_agent(
        model=llm,
        tools=reporting_tools,
        name="reporting_agent",
        prompt=REPORTING_AGENT_PROMPT,
    )

    estate_agent = create_react_agent(
        model=llm,
        tools=estate_tools,
        name="estate_agent",
        prompt=ESTATE_AGENT_PROMPT,
    )

    analysis_agent = create_react_agent(
        model=llm,
        tools=analysis_tools,
        name="analysis_agent",
        prompt=ANALYSIS_AGENT_PROMPT,
    )

    # Build supervisor graph
    workflow = create_supervisor(
        agents=[portfolio_agent, tax_agent, accounting_agent, reporting_agent, estate_agent, analysis_agent],
        model=llm,
        prompt=SUPERVISOR_PROMPT,
    )

    return workflow.compile()


def _get_default_llm():
    """Get the default LLM based on configuration."""
    from app.config import settings

    if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        try:
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=settings.llm_model,
                api_key=settings.anthropic_api_key,
            )
        except Exception:
            pass

    if settings.openai_api_key:
        try:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=settings.llm_model,
                api_key=settings.openai_api_key,
            )
        except Exception:
            pass

    return None


def _create_fallback_supervisor(session_factory):
    """
    Create a simple non-LLM supervisor for when no API key is configured.

    This allows the system to run with direct service calls via the web UI
    even without an LLM backend.
    """

    class FallbackSupervisor:
        """Fallback supervisor that explains LLM is not configured."""

        def invoke(self, state, config=None):
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": (
                            "The AI agent system requires an LLM API key to be configured. "
                            "Please set KFO_ANTHROPIC_API_KEY or KFO_OPENAI_API_KEY in your .env file. "
                            "In the meantime, you can use the web dashboard directly to manage "
                            "your portfolio, view tax reports, and handle approvals."
                        ),
                    }
                ]
            }

        async def ainvoke(self, state, config=None):
            return self.invoke(state, config)

    return FallbackSupervisor()
