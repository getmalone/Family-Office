"""
Agent chat endpoint for natural-language interaction with the Family Office.

Sends user messages to the LangGraph supervisor which routes to the appropriate
specialist agent. Also provides a direct /portfolio-advice endpoint that calls
the Anthropic API with rich portfolio context for fast, context-aware responses.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.db import get_factory

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

PORTFOLIO_ADVISOR_SYSTEM = """You are a senior investment advisor for the Family Office — \
a private, tax-efficient wealth management entity serving the Principal and their family.

You have been given a live snapshot of the portfolio including:
- All current holdings with market values, cost basis, and unrealized P&L
- Asset class allocation breakdown
- Overall portfolio metrics (AUM, total return)

Your role is to provide thoughtful, specific, and actionable investment guidance. \
When asked about new investments or recommendations, be concrete — name specific \
tickers or asset classes with clear rationale. Consider the existing allocation, \
concentration risk, tax efficiency, and long-term wealth preservation goals. \
Always note that this is general financial information, not personalised regulated advice.

Respond in clear, well-structured prose. Use bullet points for lists of recommendations. \
Keep responses focused and practical — the user is a sophisticated investor."""


@router.post("/invoke")
async def invoke_agent(
    request: Request,
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    """Send a message to the supervisor agent and get a response."""
    from app.agents.supervisor import build_supervisor

    factory = get_factory()
    supervisor = build_supervisor(factory)

    try:
        result = supervisor.invoke(
            {"messages": [{"role": "user", "content": message}]}
        )

        # Extract the last assistant message
        messages = result.get("messages", [])
        response_text = "No response from agent."
        for msg in reversed(messages):
            content = getattr(msg, "content", None) or msg.get("content", "")
            role = getattr(msg, "type", None) or msg.get("role", "")
            if role in ("ai", "assistant") and content:
                response_text = content
                break

        return JSONResponse({"response": response_text})

    except Exception as e:
        return JSONResponse(
            {"response": f"Agent error: {str(e)}"},
            status_code=500,
        )


@router.post("/portfolio-advice")
async def portfolio_advice(
    request: Request,
    message: str = Form(...),
    context: str = Form(default=""),
):
    """
    Direct Anthropic call with portfolio context injected into the system prompt.
    Faster than the full LangGraph supervisor — no tool calls needed since the
    data is already embedded in the context.
    """
    import os
    from pathlib import Path
    from app.config import settings

    api_key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    # Last-resort: read directly from .env file in the project root
    if not api_key:
        env_file = Path(__file__).parent.parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("KFO_ANTHROPIC_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val and val != "your-anthropic-api-key-here":
                        api_key = val
                        break

    if not api_key:
        return JSONResponse(
            {"response": "No Anthropic API key configured. Add KFO_ANTHROPIC_API_KEY to your .env file."},
            status_code=200,
        )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        # Build the full system prompt with live portfolio data embedded
        system = PORTFOLIO_ADVISOR_SYSTEM
        if context.strip():
            system += f"\n\n--- LIVE PORTFOLIO SNAPSHOT ---\n{context.strip()}\n--- END SNAPSHOT ---"

        response = client.messages.create(
            model=settings.llm_model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": message}],
        )

        reply = response.content[0].text if response.content else "No response."
        return JSONResponse({"response": reply})

    except Exception as e:
        return JSONResponse(
            {"response": f"Advisor error: {str(e)}"},
            status_code=200,
        )
