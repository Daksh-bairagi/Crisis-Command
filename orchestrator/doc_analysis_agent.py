"""
ADK-based incident document analysis agent.

Uses Google ADK's LlmAgent with five tools spanning RAG, GitHub, Cloud Logging,
Chat, and Docs MCP servers to investigate a production incident and produce a
specific, actionable analysis. The agent decides the order of tool calls based
on the incident context, synthesizes findings, optionally writes them to the
incident doc, and posts a concise summary to the on-call Chat space.

The agent is fire-and-forget: any failure is swallowed (returns empty string)
so the orchestrator's critical path is never blocked.
"""

import asyncio
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from database.db import search_similar_incidents
from orchestrator.mcp_clients import create_mcp_client
from logger import get_logger
from agents.chat_agent import actions as chat_actions
from agents.docs_agent import actions as docs_actions

log = get_logger("doc_analysis_agent")

_EMBED_TIMEOUT = float(os.getenv("EXTERNAL_CALL_TIMEOUT_SECONDS", "10"))
_MCP_TIMEOUT = float(os.getenv("EXTERNAL_CALL_TIMEOUT_SECONDS", "8"))


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


EXTERNAL_ACTIONS_ENABLED = _env_flag("ENABLE_EXTERNAL_ACTIONS", bool(os.getenv("K_SERVICE")))
GENAI_FEATURES_ENABLED = _env_flag("ENABLE_GENAI_FEATURES", bool(os.getenv("K_SERVICE")))


_AGENT_INSTRUCTION = """You are an expert SRE incident analyst. You have been given a new production incident.
Your job: investigate it thoroughly and produce a specific, actionable analysis.

INVESTIGATION WORKFLOW:
1. If deployment_id is present in the incident details, call get_deployment_info(deployment_id) to check what changed.
2. Call search_recent_logs(service_name, query) with relevant error keywords from the alert description.
3. Call search_past_incidents(query) to find similar past incidents and their resolutions.
4. Synthesize your findings into a specific analysis covering:
   - SPECIFIC CAUSE: What exactly is broken (not generic — cite log lines, deployment changes, past patterns)
   - DEPLOYMENT ASSESSMENT: Was a recent deployment causal? What changed?
   - INVESTIGATION STEPS: 3-5 concrete next steps for the on-call engineer
   - LIKELY RESOLUTION: Based on past incidents and current evidence, what is most likely to fix this fast?
5. If doc_id is provided, call update_doc_analysis(doc_id, your_analysis) to write findings to the incident doc.
6. Call post_chat_message with a concise summary (3-4 lines max) for the on-call team.

Be specific. Cite actual log lines, deployment details, and past incident resolutions. Never say "check the logs" — tell them WHAT to look for and WHERE."""


# ---------------------------------------------------------------------------
# Async helpers (run from inside sync tool functions via run_until_complete)
# ---------------------------------------------------------------------------

async def _embed_query(text: str) -> list[float] | None:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        response = await asyncio.wait_for(
            asyncio.to_thread(
                genai.embed_content,
                model="models/gemini-embedding-001",
                content=text,
                task_type="RETRIEVAL_QUERY",
            ),
            timeout=_EMBED_TIMEOUT,
        )
        return response["embedding"]
    except Exception as e:
        log.warning("Embedding failed in analysis agent: %s", e)
        return None


async def _rag_search(query: str) -> dict:
    embedding = await _embed_query(query)
    if not embedding:
        return {"incidents": [], "count": 0, "error": "embedding unavailable"}
    try:
        results = await search_similar_incidents(embedding, limit=3)
        return {"incidents": results or [], "count": len(results or [])}
    except Exception as e:
        return {"incidents": [], "count": 0, "error": str(e)}


async def _call_mcp_tool(script_path: str, name: str, tool: str, args: dict) -> dict:
    client = create_mcp_client(script_path, name)
    async with client:
        result = await client.call_tool(tool, args, timeout=_MCP_TIMEOUT)
    return _result_to_dict(result)


def _result_to_dict(result) -> dict:
    if isinstance(getattr(result, "data", None), dict):
        return result.data
    if isinstance(getattr(result, "structured_content", None), dict):
        return result.structured_content
    return {
        "success": not getattr(result, "is_error", True),
        "content": getattr(result, "content", []),
    }


def _run_async(coro):
    """Run an async coroutine from a sync ADK tool function.

    ADK tool functions are synchronous but execute on the same thread as the
    running ADK event loop. asyncio.run() refuses to nest loops on the same
    thread, so we create a brand-new event loop, run it to completion, then
    close it. Both loops run serially on the calling thread — no new thread is
    created.
    """
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except Exception as e:
        log.warning("Async run from sync tool failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# ADK tools (must be sync `def`)
# ---------------------------------------------------------------------------

def search_past_incidents(query: str) -> dict:
    """Search past incidents similar to the given error pattern or query.
    Use this to find how similar incidents were resolved.

    Args:
        query: Error pattern, service name, or symptom description to search for.

    Returns:
        {"incidents": list of past incidents, "count": int}
    """
    try:
        return _run_async(_rag_search(query))
    except Exception as e:
        log.warning("search_past_incidents tool failed: %s", e)
        return {"incidents": [], "count": 0, "error": str(e)}


def get_deployment_info(deployment_id: str) -> dict:
    """Fetch deployment details (PR title, author, files changed, diff summary)
    for a given deployment_id from the GitHub MCP server.

    Use this when an incident references a deployment to see what code changed.

    Args:
        deployment_id: Deployment identifier (e.g., 'deploy-447').

    Returns:
        Raw result dict from the GitHub MCP `get_deployment_info` tool.
    """
    try:
        return _run_async(
            _call_mcp_tool(
                "agents/github_agent/mcp_server.py",
                "github-mcp",
                "get_deployment_info",
                {"deployment_id": deployment_id},
            )
        )
    except Exception as e:
        log.warning("get_deployment_info tool failed: %s", e)
        return {"success": False, "error": str(e)}


def search_recent_logs(service_name: str, query: str) -> dict:
    """Search recent Cloud Logging entries for a service.

    Use this with error keywords from the alert description to confirm
    error frequency, affected pods, and timing relative to a deployment.

    Args:
        service_name: Service name (e.g., 'payments-service').
        query: Log filter — error keywords or symptom phrases.

    Returns:
        Raw result dict from the Cloud Logging MCP `search_logs` tool.
    """
    try:
        return _run_async(
            _call_mcp_tool(
                "agents/logging_agent/mcp_server.py",
                "logging-mcp",
                "search_logs",
                {"service_name": service_name, "query": query, "minutes_back": 15},
            )
        )
    except Exception as e:
        log.warning("search_recent_logs tool failed: %s", e)
        return {"success": False, "error": str(e)}


def post_chat_message(text: str) -> dict:
    """Post a concise summary message to the on-call Google Chat space.

    Args:
        text: Short (3-4 line) summary for the on-call team.

    Returns:
        {"success": bool, "error": str (optional)}
    """
    if not EXTERNAL_ACTIONS_ENABLED:
        return {"success": False, "error": "external actions disabled"}
    try:
        return chat_actions.post_text_message(text)
    except Exception as e:
        log.warning("post_chat_message tool failed: %s", e)
        return {"success": False, "error": str(e)}


def update_doc_analysis(doc_id: str, content: str) -> dict:
    """Write the analysis findings into the incident doc's INCIDENT ANALYSIS section.

    Args:
        doc_id: Google Doc ID of the incident document.
        content: The synthesized analysis text to insert.

    Returns:
        {"success": bool, "error": str (optional)}
    """
    if not EXTERNAL_ACTIONS_ENABLED:
        return {"success": False, "error": "external actions disabled"}
    try:
        return docs_actions.update_doc_section(doc_id, "INCIDENT ANALYSIS", content)
    except Exception as e:
        log.warning("update_doc_analysis tool failed: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def generate_doc_analysis(alert: dict, classification, doc_id: str = None) -> str:
    """
    Run the ADK incident analysis agent. Returns the final analysis text
    (also written to the incident doc via the agent's tool calls).

    Fire-and-forget: any failure is swallowed and an empty string is returned.
    The orchestrator's critical path must never crash because Phase 2 failed.
    """
    if not GENAI_FEATURES_ENABLED:
        log.info("GenAI features disabled — skipping ADK doc analysis")
        return ""

    if not os.getenv("GOOGLE_API_KEY"):
        log.warning("GOOGLE_API_KEY not set — skipping ADK doc analysis")
        return ""

    try:
        agent = LlmAgent(
            name="incident_doc_analyst",
            model="gemini-2.0-flash",
            tools=[
                search_past_incidents,
                get_deployment_info,
                search_recent_logs,
                post_chat_message,
                update_doc_analysis,
            ],
            instruction=_AGENT_INSTRUCTION,
        )

        session_service = InMemorySessionService()
        runner = Runner(
            agent=agent,
            app_name="crisiscommand_doc_analysis",
            session_service=session_service,
        )

        diagnostics = alert.get("diagnostics", {}) or {}
        logs = diagnostics.get("last_logs", []) or []
        logs_text = "\n".join(f"  {line}" for line in logs) if logs else "  (no log data provided)"
        dep_age = diagnostics.get("deployment_age_minutes")
        dep_label = f"{dep_age} minutes before incident" if dep_age is not None else "age unknown"
        # "" is treated as absent and falls through to diagnostics — intentional
        deployment_id = getattr(classification, "deployment_id", None) or diagnostics.get("last_deployment", "unknown")

        doc_id_line = f"doc_id: {doc_id}" if doc_id else "doc_id: (none — skip update_doc_analysis)"

        user_message = (
            f"Incident: {classification.incident_id} | Severity: {classification.severity}\n"
            f"Service: {classification.service} | Region: {classification.region}\n"
            f"Error Rate: {float(classification.error_rate or 0) * 100:.0f}%"
            f" | Affected Users: {classification.affected_users}"
            f" | P99 Latency: {alert.get('latency_p99_ms', 'N/A')}ms"
            f" | Requests/min: {alert.get('requests_per_minute', 'N/A')}\n\n"
            f"Description: {classification.description}\n\n"
            f"deployment_id: {deployment_id}\n"
            f"Last Deployment: {diagnostics.get('last_deployment', 'unknown')} ({dep_label})\n"
            f"CPU: {diagnostics.get('cpu_usage', 'N/A')}"
            f" | Memory: {diagnostics.get('memory_usage', 'N/A')}\n\n"
            f"Error Logs (snippet from alert):\n{logs_text}\n\n"
            f"{doc_id_line}\n\n"
            f"Investigate this incident. Call the tools in the workflow order, "
            f"then write the analysis to the doc and post a summary to chat."
        )

        session = await session_service.create_session(
            app_name="crisiscommand_doc_analysis",
            user_id="orchestrator",
        )

        final_text = ""
        async for event in runner.run_async(
            user_id="orchestrator",
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text=user_message)]),
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        final_text += part.text

        final_text = final_text.strip()
        if final_text:
            log.info(
                "ADK analysis generated for %s (%d chars)",
                classification.incident_id,
                len(final_text),
            )
        else:
            log.warning("ADK agent returned empty response for %s", classification.incident_id)
        return final_text

    except Exception as e:
        log.error("ADK doc analysis failed for %s: %s", classification.incident_id, e)
        return ""
