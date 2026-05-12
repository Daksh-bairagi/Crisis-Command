"""
CrisisCommand Orchestrator - Multi-Agent Incident Response

Main coordinator that orchestrates incident response by calling action modules directly.
"""

import os
import sys
import asyncio
import json
import uuid

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()

from logger import get_logger
from orchestrator.classifier import classify, IncidentClassification
from orchestrator.doc_analysis_agent import generate_doc_analysis
from database.db import (
    store_incident,
    store_incident_memory,
    search_similar_incidents,
    log_trace,
    get_active_incident_for_service,
)

from agents.chat_agent.actions import post_incident_alert as chat_post_incident_alert
from agents.chat_agent.actions import post_text_message as chat_post_text_message
from agents.docs_agent.actions import create_incident_doc as docs_create_incident_doc
from agents.calendar_agent.actions import block_oncall_calendar as cal_block_calendar
from agents.calendar_agent.actions import create_meet_link as cal_create_meet_link

import google.generativeai as genai

log = get_logger("orchestrator")
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
EXTERNAL_CALL_TIMEOUT_SECONDS = float(os.getenv("EXTERNAL_CALL_TIMEOUT_SECONDS", "8"))


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


EXTERNAL_ACTIONS_ENABLED = _env_flag("ENABLE_EXTERNAL_ACTIONS", bool(os.getenv("K_SERVICE")))
GENAI_FEATURES_ENABLED = _env_flag("ENABLE_GENAI_FEATURES", bool(os.getenv("K_SERVICE")))
DATABASE_ENABLED = _env_flag("ENABLE_DATABASE", bool(os.getenv("K_SERVICE")))

# Strong references to fire-and-forget background tasks so they are not GC'd mid-flight.
# Standard pattern from Python asyncio docs.
_background_tasks: set[asyncio.Task] = set()


# ─── DISPATCH HELPERS ────────────────────────────────────────────

async def _dispatch_chat(classification, similar_text: str, session_id: str) -> dict:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                chat_post_incident_alert,
                incident_id=classification.incident_id,
                severity=classification.severity,
                service=classification.service,
                description=classification.description,
                likely_cause=classification.likely_cause,
                suggested_action=classification.suggested_action,
                affected_users=classification.affected_users,
                region=classification.region,
                doc_link="",
                meet_link="",
                similar_incidents=similar_text or "",
            ),
            timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
        )
        log.info("[%s] Posted to Google Chat: %s", session_id, result)
        return {"message_id": result.get("message_name", f"msg-{classification.incident_id}"), "success": result.get("success", False)}
    except Exception as e:
        log.error("[%s] Chat dispatch failed: %s", session_id, e)
        return {"success": False, "error": str(e)}


async def _dispatch_docs(classification, similar_text: str, alert: dict, session_id: str) -> dict:
    try:
        diagnostics = alert.get("diagnostics", {})
        error_logs = "\n".join(diagnostics.get("last_logs", []))
        result = await asyncio.wait_for(
            asyncio.to_thread(
                docs_create_incident_doc,
                incident_id=classification.incident_id,
                severity=classification.severity,
                service=classification.service,
                description=classification.description,
                likely_cause=classification.likely_cause,
                suggested_action=classification.suggested_action,
                affected_users=classification.affected_users,
                region=classification.region,
                error_rate=classification.error_rate,
                latency_p99_ms=str(alert.get("latency_p99_ms", "N/A")),
                requests_per_minute=str(alert.get("requests_per_minute", "N/A")),
                deployment_id=classification.deployment_id,
                deployment_age_minutes=str(diagnostics.get("deployment_age_minutes", "unknown")),
                cpu_usage=diagnostics.get("cpu_usage", "N/A"),
                memory_usage=diagnostics.get("memory_usage", "N/A"),
                error_logs=error_logs,
                similar_incidents=similar_text,
                analysis="",
            ),
            timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
        )
        log.info("[%s] Created doc: %s", session_id, result)
        return {"doc_url": result.get("doc_url", ""), "success": result.get("success", False)}
    except Exception as e:
        log.error("[%s] Docs dispatch failed: %s", session_id, e)
        return {"success": False, "error": str(e)}


async def _dispatch_calendar(classification, session_id: str) -> dict:
    try:
        block_result, meet_result = await asyncio.gather(
            asyncio.wait_for(
                asyncio.to_thread(
                    cal_block_calendar,
                    classification.incident_id,
                    classification.service,
                    classification.severity,
                    120,
                ),
                timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
            ),
            asyncio.wait_for(
                asyncio.to_thread(
                    cal_create_meet_link,
                    classification.incident_id,
                    classification.service,
                    classification.severity,
                ),
                timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
            ),
            return_exceptions=True,
        )
        if isinstance(block_result, Exception):
            log.error("[%s] block_oncall_calendar failed: %s", session_id, block_result)
        else:
            log.info("[%s] Blocked calendar: %s", session_id, block_result)
        if isinstance(meet_result, Exception):
            log.error("[%s] create_meet_link failed: %s", session_id, meet_result)
            return {"success": False, "error": str(meet_result)}
        log.info("[%s] Created Meet: %s", session_id, meet_result)
        return {"meet_url": meet_result.get("meet_url", ""), "success": meet_result.get("success", False)}
    except Exception as e:
        log.error("[%s] Calendar dispatch failed: %s", session_id, e)
        return {"success": False, "error": str(e)}


async def _post_meet_to_chat(classification, meet_url: str, session_id: str):
    try:
        message = (
            f"🔗 **WAR ROOM**: {meet_url}\n\n"
            f"Join the Google Meet to coordinate response for "
            f"[{classification.severity}] {classification.service} incident."
        )
        await asyncio.wait_for(
            asyncio.to_thread(chat_post_text_message, message),
            timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
        )
        log.info("[%s] Posted Meet link to Chat", session_id)
    except Exception as e:
        log.error("[%s] Failed to post Meet to Chat: %s", session_id, e)


# ─── PRIVATE HELPERS FOR EMBEDDING ───────────────────────────────

async def _search_similar_incidents(alert: dict, session_id: str) -> list[dict]:
    """Generate embedding using gemini-embedding-001"""
    if not os.getenv("GOOGLE_API_KEY") or not GENAI_FEATURES_ENABLED:
        return []
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                genai.embed_content,
                model="models/gemini-embedding-001",
                content=f"{alert.get('service')}: {alert.get('description')}",
                task_type="RETRIEVAL_DOCUMENT",
            ),
            timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
        )
        embedding = response['embedding']
        results = await search_similar_incidents(embedding, limit=3)
        return results
    except Exception as e:
        log.error("[%s] Embedding failed: %s", session_id, e)
        return []


async def _create_incident_memory(classification: IncidentClassification, session_id: str):
    """Store incident as memory for future RAG"""
    if not os.getenv("GOOGLE_API_KEY") or not GENAI_FEATURES_ENABLED:
        return
    try:
        memory_text = f"[{classification.severity}] {classification.service}: {classification.description} - {classification.likely_cause}"
        response = await asyncio.wait_for(
            asyncio.to_thread(
                genai.embed_content,
                model="models/gemini-embedding-001",
                content=memory_text,
                task_type="RETRIEVAL_DOCUMENT",
            ),
            timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
        )
        await store_incident_memory(
            incident_id=classification.incident_id,
            content=memory_text,
            embedding=response['embedding'],
            source="orchestration"
        )
    except Exception as e:
        log.warning("[%s] Could not store memory: %s", session_id, e)


# ─── PUBLIC API ───────────────────────────────────────────────────

async def process_incident_alert(alert: dict) -> dict:
    """Main orchestration flow. Called by webhook/main.py."""
    session_id = f"session-{str(uuid.uuid4())[:8]}"

    try:
        log.info("[%s] Orchestrating: %s", session_id, alert.get("service"))

        # Deduplication: if an active P0/P1 already exists for this service,
        # suppress the new alert. Alert storms on a broken service should not
        # produce 50 duplicate Chat cards, Docs, and Calendar events.
        if DATABASE_ENABLED:
            existing = await get_active_incident_for_service(alert.get("service", ""))
            if existing and existing.get("severity") in ("P0", "P1"):
                log.info(
                    "[%s] Duplicate suppressed — active %s incident %s already exists for %s.",
                    session_id,
                    existing["severity"],
                    existing["incident_id"],
                    alert.get("service"),
                )
                return {
                    "success": True,
                    "deduplicated": True,
                    "incident_id": existing["incident_id"],
                    "severity": existing["severity"],
                    "service": alert.get("service"),
                    "message": "Duplicate alert suppressed — incident already active.",
                }

        # Step 1: Classify
        log.info("[%s] Step 1: Classifying alert...", session_id)
        classification = await classify(alert)
        log.info("[%s] -> %s: %s", session_id, classification.severity, classification.likely_cause)

        # Step 2: Search for similar incidents
        log.info("[%s] Step 2: Searching similar incidents...", session_id)
        similar = await _search_similar_incidents(alert, session_id) if DATABASE_ENABLED else []
        similar_text = similar[0]["content"] if similar else ""

        # Step 3: Store incident
        if DATABASE_ENABLED:
            log.info("[%s] Step 3: Storing incident...", session_id)
            if not await store_incident(classification):
                log.warning(
                    "[%s] Persistent storage unavailable. Continuing in local/degraded mode.",
                    session_id,
                )
        else:
            log.info("[%s] Step 3: Database disabled in local mode.", session_id)

        # Step 4: Create memory for RAG
        if DATABASE_ENABLED:
            await _create_incident_memory(classification, session_id)

        # Step 5: Dispatch actions
        log.info("[%s] Step 5: Dispatching actions...", session_id)
        response = {
            "success": True,
            "incident_id": classification.incident_id,
            "severity": classification.severity,
            "service": classification.service,
            "chat_message": None,
            "doc_url": None,
            "meet_url": None,
            "similar_incident": similar[0] if similar else None,
        }

        if EXTERNAL_ACTIONS_ENABLED:
            # Build coroutine map based on severity, then dispatch in parallel.
            # asyncio.gather fires Chat + Docs + Calendar simultaneously instead of
            # sequentially (which would stack up to 3x the timeout on the critical path).
            coros: dict = {}
            if classification.activate_chat:
                coros["chat"] = _dispatch_chat(classification, similar_text, session_id)
            if classification.activate_docs:
                coros["docs"] = _dispatch_docs(classification, similar_text, alert, session_id)
            if classification.activate_calendar:
                coros["calendar"] = _dispatch_calendar(classification, session_id)

            if coros:
                log.info(
                    "[%s] Dispatching %s in parallel...",
                    session_id,
                    list(coros.keys()),
                )
                names = list(coros.keys())
                gathered = await asyncio.gather(*coros.values(), return_exceptions=True)

                meet_url: str | None = None
                for name, result in zip(names, gathered):
                    if isinstance(result, Exception):
                        log.error("[%s] %s agent raised: %s", session_id, name, result)
                        continue
                    if name == "chat":
                        response["chat_message"] = result.get("message_id")
                    elif name == "docs":
                        response["doc_url"] = result.get("doc_url")
                    elif name == "calendar":
                        response["meet_url"] = result.get("meet_url")
                        if result.get("success") and result.get("meet_url"):
                            meet_url = result["meet_url"]

                if meet_url:
                    await _post_meet_to_chat(classification, meet_url, session_id)
        else:
            log.info(
                "[%s] External actions disabled. Skipping Chat/Docs/Calendar side effects.",
                session_id,
            )

        # ─── PHASE 2: Background incident analysis ────────────────────
        # The ADK doc-analysis agent runs GitHub + Cloud Logging + RAG investigations,
        # writes findings to the doc, and posts a summary to Chat.
        # Fire-and-forget: orchestrator response is not blocked on this.
        if GENAI_FEATURES_ENABLED:
            doc_url = response.get("doc_url")
            doc_id = _extract_doc_id(doc_url) if doc_url else None
            log.info(
                "[%s] Phase 2: dispatching ADK incident analysis (doc_id=%s)",
                session_id,
                doc_id or "none",
            )
            _task = asyncio.create_task(
                generate_doc_analysis(alert, classification, doc_id=doc_id)
            )
            _background_tasks.add(_task)
            _task.add_done_callback(_background_tasks.discard)

        # Log trace
        if DATABASE_ENABLED:
            trace_logged = await log_trace(
                session_id,
                "orchestrator",
                "process_incident_alert",
                input_data={"alert": alert},
                output_data=response,
            )
            if not trace_logged:
                log.warning("[%s] Trace logging unavailable.", session_id)

        log.info("[%s] Done: %s", session_id, classification.incident_id)
        return response

    except Exception as e:
        log.error("[%s] Failed: %s", session_id, str(e))
        return {"success": False, "error": str(e)}


# ─── UTILITY FUNCTIONS ────────────────────────────────────────────

def _extract_doc_id(doc_url: str | None) -> str | None:
    """Extract Google Doc ID from a URL like https://docs.google.com/document/d/{ID}/edit"""
    if not doc_url:
        return None
    try:
        parts = doc_url.split("/d/")
        if len(parts) >= 2:
            return parts[1].split("/")[0]
    except Exception:
        pass
    return None


if __name__ == "__main__":
    test_alert = {
        "service": "payments-service",
        "error_rate": 0.85,
        "description": "Checkout returning 500s",
        "affected_users": 5000,
        "region": "us-east1"
    }
    result = asyncio.run(process_incident_alert(test_alert))
    print(json.dumps(result, indent=2))
