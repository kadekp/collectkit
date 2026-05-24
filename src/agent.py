"""
LangGraph collectkit agent.

Persona, prompts, and status-context strategies are loaded from external
config (see `src.config`). Domain logic (PTP tools, borrower data) stays
in code; product-specific text lives in YAML/Markdown bundles so the same
agent can serve many products with no code changes.
"""

from __future__ import annotations

import operator
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from typing_extensions import Annotated, TypedDict

from .config import get_config
from .database_pg import BorrowerDetails, get_max_ptp_days
from .i18n import format_currency, format_date_iso
from .logging_config import get_logger
from .timezone_utils import today_local

logger = get_logger(__name__)

load_dotenv()


# ============================================================================
# PROMPT TEMPLATE LOADER
# ============================================================================

def _load_prompt_template() -> str:
    """Load the system prompt template from PROMPT_DIR/system_prompt.md."""
    cfg = get_config()
    prompt_path = Path(cfg.prompt_dir) / "system_prompt.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"System prompt template not found at {prompt_path}. "
            "Set PROMPT_DIR to a directory containing system_prompt.md."
        )


# Cache the template at module load time
SYSTEM_PROMPT_TEMPLATE = _load_prompt_template()


# ============================================================================
# STATE SCHEMA
# ============================================================================

class ChatState(TypedDict):
    """LangGraph state schema with message reducer for proper appending."""
    messages: Annotated[list[BaseMessage], operator.add]
    user_phone: str
    borrower_details: Optional[BorrowerDetails]


# ============================================================================
# TOOLS FACTORY
# ============================================================================

def create_tools(
    user_phone: str,
    record_ptp_fn: Callable,
    get_ptp_history_fn: Callable,
    set_handoff_fn: Callable,
    schedule_payment_check_fn: Callable = None,
    borrower_details: "BorrowerDetails" = None,
):
    """Create tools bound to the current user context."""
    cfg = get_config()
    bot_name = cfg.bot_name

    @tool
    def record_promise_to_pay(promise_amount: float, promise_date: str) -> str:
        """
        Record a Promise to Pay (PTP) from the customer.

        Args:
            promise_amount: Amount the customer promises to pay
            promise_date: Date they promise to pay by (YYYY-MM-DD)

        Returns:
            Confirmation message with PTP details
        """
        return record_ptp_fn(user_phone, promise_amount, promise_date)

    @tool
    def get_ptp_history() -> str:
        """
        Look up the customer's previous Promises to Pay.
        Use this when the customer asks about their prior commitments.

        Returns:
            Text summary of past PTPs
        """
        ptps = get_ptp_history_fn(user_phone)
        if not ptps:
            return "No previous Promises to Pay on record."

        lines = ["Promise to Pay history:"]
        for ptp in ptps:
            lines.append(
                f"- {format_currency(ptp.promise_amount)} on {ptp.promise_date} "
                f"(Status: {ptp.status})"
            )
        return "\n".join(lines)

    @tool
    def request_human_handoff(reason: str) -> str:
        """
        Hand off the conversation to a human support agent.
        Use when the customer:
        - explicitly asks for a human / CS / operator
        - is repeatedly frustrated or angry
        - presents a problem you can't resolve after a few turns
        - has a complex situation that needs human judgment

        Args:
            reason: Short reason for the handoff

        Returns:
            Confirmation that the handoff has been recorded
        """
        success = set_handoff_fn(user_phone, reason)
        if success:
            return f"✓ Human handoff requested. Reason: {reason}. Session marked for human takeover."
        return "✗ Failed to request human handoff. Session not found."

    @tool
    def schedule_payment_verification() -> str:
        """
        Schedule an automatic payment-status verification.
        Use this when the customer says they have ALREADY paid.

        Do NOT use this when the customer is only planning to pay
        (use record_promise_to_pay for plans).

        Returns:
            Confirmation that verification has been scheduled
        """
        if schedule_payment_check_fn is None:
            return "✗ Payment verification scheduling not available."
        if borrower_details is None:
            return "✗ Cannot schedule verification: customer data not found."

        from .database_pg import has_pending_payment_check
        if has_pending_payment_check(user_phone):
            return f"✓ A payment verification is already pending. {bot_name} will follow up when it completes."

        try:
            task_id = schedule_payment_check_fn(
                phone_number=user_phone,
                customer_number=borrower_details.customer_number,
                task_type="payment_check",
                delay_hours=1.0,
            )
            return f"✓ Payment verification scheduled (ID: {task_id}). {bot_name} will check shortly."
        except Exception as e:
            return f"✗ Failed to schedule verification: {e}"

    tools = [record_promise_to_pay, get_ptp_history, request_human_handoff]
    if schedule_payment_check_fn is not None:
        tools.append(schedule_payment_verification)
    return tools


# ============================================================================
# SYSTEM PROMPT BUILDER
# ============================================================================

def _select_strategy(
    tiers: list[dict[str, Any]], value: int, key: str
) -> dict[str, Any]:
    """Pick the first tier whose `key` is None (catch-all) or >= value."""
    for tier in tiers:
        cap = tier.get(key)
        if cap is None:
            return tier
        if value <= int(cap):
            return tier
    return tiers[-1] if tiers else {}


def _build_status_context(borrower: BorrowerDetails) -> str:
    """Render the status-context block using strategies.yaml templates."""
    cfg = get_config()
    strategies = cfg.strategies or {}

    base_vars = {
        **cfg.template_vars(),
        "amount": format_currency(borrower.billing_amount),
        "days_late": borrower.days_late,
        "days_until_due": abs(borrower.days_late),
        "customer_name": borrower.customer_name or "",
    }

    status = borrower.status
    bucket = strategies.get(status)

    if isinstance(bucket, list):
        # Tiered: pick the right tier
        key = "max_days_until_due" if status == "UPCOMING" else "max_days"
        value = abs(borrower.days_late) if status == "UPCOMING" else borrower.days_late
        tier = _select_strategy(bucket, value, key)
        template = tier.get("template", "")
    elif isinstance(bucket, dict):
        # Single template (e.g. PAID)
        template = bucket.get("template", "")
    else:
        return ""

    try:
        return template.format(**base_vars).strip()
    except KeyError as missing:
        logger.warning(
            "Strategy template references undefined placeholder",
            extra={"event": "strategy_template_error", "missing": str(missing)},
        )
        return template


def build_system_prompt(borrower: Optional[BorrowerDetails]) -> str:
    """Build the system prompt for the current borrower (or unregistered fallback)."""
    cfg = get_config()

    if not borrower:
        fallback = (cfg.strategies or {}).get("unregistered_fallback", "")
        if fallback:
            try:
                return fallback.format(**cfg.template_vars()).strip()
            except KeyError:
                return fallback.strip()
        return (
            f"You are {cfg.bot_name}, a customer-care assistant for {cfg.company_name}. "
            "Sorry — this phone number isn't registered in our system."
        )

    borrower_json = borrower.model_dump_json(indent=2)
    status_context = _build_status_context(borrower)

    loan_details = ""
    for i, loan in enumerate(borrower.loans, 1):
        loan_details += (
            f"  {i}. Date: {loan.loan_date}, Amount: {format_currency(loan.loan_amount)}\n"
        )

    max_ptp_days = get_max_ptp_days(borrower.days_late)
    today_date = format_date_iso(today_local())

    placeholders = {
        **cfg.template_vars(),
        "today_date": today_date,
        "borrower_json": borrower_json,
        "status_context": status_context,
        "loan_details": loan_details,
        "max_ptp_days": max_ptp_days,
        "customer_number": borrower.customer_number,
        "days_late": borrower.days_late,
    }

    try:
        return SYSTEM_PROMPT_TEMPLATE.format(**placeholders)
    except KeyError as missing:
        raise KeyError(
            f"System prompt references undefined placeholder {missing}. "
            f"Available: {sorted(placeholders.keys())}"
        ) from missing


# ============================================================================
# LANGGRAPH NODES (parameterized with database functions)
# ============================================================================

def create_loader_node(fetch_borrower_fn: Callable):
    """Create loader node with configurable database function."""
    def loader_node(state: ChatState) -> dict:
        borrower = fetch_borrower_fn(state["user_phone"])
        return {"borrower_details": borrower}
    return loader_node


def create_agent_node(
    record_ptp_fn: Callable,
    get_ptp_history_fn: Callable,
    set_handoff_fn: Callable,
    schedule_payment_check_fn: Callable = None,
):
    """Create the LLM agent node."""
    cfg = get_config()

    def agent_node(state: ChatState) -> dict:
        borrower = state["borrower_details"]
        user_phone = state["user_phone"]

        system_prompt = build_system_prompt(borrower)

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key or api_key == "your-openrouter-api-key":
            raise ValueError(
                "Invalid OPENROUTER_API_KEY. Set a valid API key in your .env file. "
                "Current value is missing or is the default placeholder."
            )

        llm = ChatOpenAI(
            model=os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.1-fast"),
            api_key=api_key,
            base_url=os.getenv("OPENROUTER_BASE_URL"),
            temperature=0.3,
            tags=["collectkit", cfg.bot_name.lower(), cfg.product_name.lower()],
        )

        tools = create_tools(
            user_phone,
            record_ptp_fn,
            get_ptp_history_fn,
            set_handoff_fn,
            schedule_payment_check_fn,
            borrower,
        )
        llm_with_tools = llm.bind_tools(tools)

        messages = [SystemMessage(content=system_prompt)] + state["messages"]

        model_name = os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.1-fast")
        logger.info("LLM request started", extra={
            "event": "llm_request_start",
            "phone_number": user_phone,
            "model": model_name,
            "message_count": len(messages),
        })

        start_time = time.time()
        response = llm_with_tools.invoke(messages)
        elapsed = time.time() - start_time

        token_usage = getattr(response, "usage_metadata", None) or {}
        has_tool_calls = bool(getattr(response, "tool_calls", None))

        logger.info("LLM response received", extra={
            "event": "llm_response",
            "phone_number": user_phone,
            "duration_ms": round(elapsed * 1000),
            "has_tool_calls": has_tool_calls,
            "input_tokens": token_usage.get("input_tokens"),
            "output_tokens": token_usage.get("output_tokens"),
            "total_tokens": token_usage.get("total_tokens"),
        })

        return {"messages": [response]}
    return agent_node


def create_tools_node(
    record_ptp_fn: Callable,
    get_ptp_history_fn: Callable,
    set_handoff_fn: Callable,
    schedule_payment_check_fn: Callable = None,
):
    """Create the tools-execution node."""
    def tools_node(state: ChatState) -> dict:
        user_phone = state["user_phone"]
        borrower = state["borrower_details"]
        last_message = state["messages"][-1]

        tools = create_tools(
            user_phone,
            record_ptp_fn,
            get_ptp_history_fn,
            set_handoff_fn,
            schedule_payment_check_fn,
            borrower,
        )
        tools_by_name = {t.name: t for t in tools}

        tool_messages = []
        for tool_call in last_message.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]

            logger.info("Tool execution started", extra={
                "event": "tool_execution",
                "phone_number": user_phone,
                "tool_name": tool_name,
                "tool_args": tool_args,
            })

            start_time = time.time()
            t = tools_by_name.get(tool_name)
            result = t.invoke(tool_args) if t else f"Error: Tool '{tool_name}' not found"
            elapsed = time.time() - start_time

            logger.info("Tool execution completed", extra={
                "event": "tool_execution_done",
                "phone_number": user_phone,
                "tool_name": tool_name,
                "duration_ms": round(elapsed * 1000),
                "success": not (result.startswith("Error:") or result.startswith("✗")),
            })

            tool_messages.append(ToolMessage(content=result, tool_call_id=tool_call["id"]))

        return {"messages": tool_messages}
    return tools_node


def should_continue(state: ChatState) -> str:
    """Route to 'tools' if the last AI message has tool_calls, else 'end'."""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "end"


# ============================================================================
# BUILD LANGGRAPH
# ============================================================================

def build_graph(
    checkpointer=None,
    fetch_borrower_fn: Callable = None,
    record_ptp_fn: Callable = None,
    get_ptp_history_fn: Callable = None,
    set_handoff_fn: Callable = None,
    schedule_payment_check_fn: Callable = None,
):
    """Build the LangGraph ReAct graph. Database functions default to PostgreSQL."""
    if fetch_borrower_fn is None:
        from .database_pg import fetch_borrower_data
        fetch_borrower_fn = fetch_borrower_data
    if record_ptp_fn is None:
        from .database_pg import record_ptp
        record_ptp_fn = record_ptp
    if get_ptp_history_fn is None:
        from .database_pg import get_ptp_history
        get_ptp_history_fn = get_ptp_history
    if set_handoff_fn is None:
        from .database_pg import set_human_handoff
        set_handoff_fn = set_human_handoff
    if schedule_payment_check_fn is None:
        from .database_pg import create_scheduled_task
        schedule_payment_check_fn = create_scheduled_task

    loader_node = create_loader_node(fetch_borrower_fn)
    agent_node = create_agent_node(
        record_ptp_fn, get_ptp_history_fn, set_handoff_fn, schedule_payment_check_fn,
    )
    tools_node = create_tools_node(
        record_ptp_fn, get_ptp_history_fn, set_handoff_fn, schedule_payment_check_fn,
    )

    graph = StateGraph(ChatState)
    graph.add_node("loader", loader_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)

    graph.add_edge(START, "loader")
    graph.add_edge("loader", "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
