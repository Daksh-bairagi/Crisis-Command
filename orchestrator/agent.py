"""
CrisisCommand Orchestrator - Multi-Agent Incident Response

Main coordinator that orchestrates incident response by calling MCP-based tools directly.
"""

import os
import sys
import asyncio
import json

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()

from logger import get_logger
from orchestrator.classifier import classify, IncidentClassification
from orchestrator.mcp_clients import create_mcp_client
from orchestrator.doc_analysis_agent import generate_doc_analysis
from database.db import (
    store_incident,
    store_incident_memory,
    search_similar_incidents,
    log_trace,
    get_active_incident_for_service,
)

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


# ─── MAIN ORCHESTRATOR AGENT ────────────────────────────────────

class IncidentOrchestrator:
    """Main orchestrator coordinating incident response"""

    def __init__(self):
        self.session_id = None
        self.chat_client = create_mcp_client("agents/chat_agent/mcp_server.py", "chat-mcp")
        self.docs_client = create_mcp_client("agents/docs_agent/mcp_server.py", "docs-mcp")
        self.calendar_client = create_mcp_client("agents/calendar_agent/mcp_server.py", "calendar-mcp")

    async def process_alert(self, alert: dict) -> dict:
        """Main orchestration flow"""
        import uuid
        self.session_id = f"session-{str(uuid.uuid4())[:8]}"
        
        try:
            log.info(f"[{self.session_id}] ▶️  Orchestrating: {alert.get('service')}")

            # Deduplication: if an active P0/P1 already exists for this service,
            # suppress the new alert. Alert storms on a broken service should not
            # produce 50 duplicate Chat cards, Docs, and Calendar events.
            if DATABASE_ENABLED:
                existing = await get_active_incident_for_service(alert.get("service", ""))
                if existing and existing.get("severity") in ("P0", "P1"):
                    log.info(
                        f"[{self.session_id}] Duplicate suppressed — active {existing['severity']} "
                        f"incident {existing['incident_id']} already exists for {alert.get('service')}."
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
            log.info(f"[{self.session_id}] Step 1: Classifying alert...")
            classification = await classify(alert)
            log.info(f"[{self.session_id}] → {classification.severity}: {classification.likely_cause}")
            
            # Step 2: Search for similar incidents
            log.info(f"[{self.session_id}] Step 2: Searching similar incidents...")
            similar = await self._search_similar_incidents(alert) if DATABASE_ENABLED else []
            similar_text = similar[0]["content"] if similar else ""
            
            # Step 3: Store incident
            if DATABASE_ENABLED:
                log.info(f"[{self.session_id}] Step 3: Storing incident...")
                if not await store_incident(classification):
                    log.warning(
                        f"[{self.session_id}] Persistent storage unavailable. Continuing in local/degraded mode."
                    )
            else:
                log.info(f"[{self.session_id}] Step 3: Database disabled in local mode.")
            
            # Step 4: Create memory for RAG
            if DATABASE_ENABLED:
                await self._create_incident_memory(classification)
            
            # Step 5: Call MCP tools
            log.info(f"[{self.session_id}] Step 5: Calling MCP tools...")
            response = {
                "success": True,
                "incident_id": classification.incident_id,
                "severity": classification.severity,
                "service": classification.service,
                "chat_message": None,
                "doc_url": None,
                "meet_url": None,
                "similar_incident": similar[0] if similar else None
            }

            if EXTERNAL_ACTIONS_ENABLED:
                # Build coroutine map based on severity, then dispatch in parallel.
                # asyncio.gather fires Chat + Docs + Calendar simultaneously instead of
                # sequentially (which would stack up to 3× the timeout on the critical path).
                coros: dict = {}
                if classification.activate_chat:
                    coros["chat"] = self._post_to_chat(classification, similar_text)
                if classification.activate_docs:
                    coros["docs"] = self._create_doc(classification, similar_text, alert)
                if classification.activate_calendar:
                    coros["calendar"] = self._block_calendar(classification)

                if coros:
                    log.info(
                        f"[{self.session_id}] Dispatching {list(coros.keys())} in parallel..."
                    )
                    names = list(coros.keys())
                    gathered = await asyncio.gather(*coros.values(), return_exceptions=True)

                    meet_url: str | None = None
                    for name, result in zip(names, gathered):
                        if isinstance(result, Exception):
                            log.error(f"[{self.session_id}] {name} agent raised: {result}")
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
                        await self._post_meet_to_chat(classification, meet_url)
            else:
                log.info(
                    f"[{self.session_id}] External actions disabled. Skipping Chat/Docs/Calendar side effects."
                )

            # ─── PHASE 2: Background incident analysis ────────────────────
            # The ADK doc-analysis agent runs GitHub + Cloud Logging + RAG investigations,
            # writes findings to the doc, and posts a summary to Chat.
            # Fire-and-forget: orchestrator response is not blocked on this.
            if GENAI_FEATURES_ENABLED:
                doc_url = response.get("doc_url")
                doc_id = _extract_doc_id(doc_url) if doc_url else None
                log.info(
                    f"[{self.session_id}] Phase 2: dispatching ADK incident analysis "
                    f"(doc_id={doc_id or 'none'})"
                )
                _task = asyncio.create_task(
                    generate_doc_analysis(alert, classification, doc_id=doc_id)
                )
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)

            # Log trace
            if DATABASE_ENABLED:
                trace_logged = await log_trace(
                    self.session_id,
                    "orchestrator",
                    "process_alert",
                    input_data={"alert": alert},
                    output_data=response,
                )
                if not trace_logged:
                    log.warning(f"[{self.session_id}] Trace logging unavailable.")
            
            log.info(f"[{self.session_id}] ✅ Done: {classification.incident_id}")
            return response
            
        except Exception as e:
            log.error(f"[{self.session_id}] ❌ Failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _search_similar_incidents(self, alert: dict) -> list[dict]:
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
            log.error(f"[{self.session_id}] Embedding failed: {e}")
            return []

    async def _create_incident_memory(self, classification: IncidentClassification):
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
            log.warning(f"[{self.session_id}] Could not store memory: {e}")

    async def _post_to_chat(self, classification: IncidentClassification, similar_text: str) -> dict:
        """Post incident to Google Chat"""
        try:
            async with self.chat_client:
                result = await self.chat_client.call_tool(
                    "post_incident_alert",
                    {
                        "incident_id": classification.incident_id,
                        "severity": classification.severity,
                        "service": classification.service,
                        "description": classification.description,
                        "likely_cause": classification.likely_cause,
                        "suggested_action": classification.suggested_action,
                        "affected_users": classification.affected_users,
                        "region": classification.region,
                        "doc_link": "",
                        "meet_link": "",
                        "similar_incidents": similar_text or "",
                    },
                    timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
            payload = _result_to_dict(result)
            log.info(f"[{self.session_id}] Posted to Google Chat via MCP: {payload}")
            return {
                "message_id": payload.get("message_name", f"msg-{classification.incident_id}"),
                "success": payload.get("success", False),
            }
        except Exception as e:
            log.error(f"[{self.session_id}] ChatAgent MCP call failed: {e}")
            return {"success": False, "error": str(e)}

    async def _post_meet_to_chat(self, classification: IncidentClassification, meet_url: str) -> dict:
        """Post Meet link to Google Chat"""
        try:
            message = f"🔗 **WAR ROOM**: {meet_url}\n\nJoin the Google Meet to coordinate response for [{classification.severity}] {classification.service} incident."
            async with self.chat_client:
                result = await self.chat_client.call_tool(
                    "post_text_message",
                    {"text": message},
                    timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
            payload = _result_to_dict(result)
            log.info(f"[{self.session_id}] Posted Meet link to Chat via MCP: {payload}")
            return payload
        except Exception as e:
            log.error(f"[{self.session_id}] Failed to post Meet to Chat via MCP: {e}")
            return {"success": False, "error": str(e)}

    async def _create_doc(self, classification: IncidentClassification, similar_text: str, alert: dict) -> dict:
        """Create incident document in Google Docs with diagnostics.

        Note: The INCIDENT ANALYSIS section is written separately in Phase 2 by the
        ADK doc-analysis agent via the docs MCP `update_doc_section` tool. We pass
        an empty `analysis` here so the doc is created fast on the critical path.
        """
        try:
            analysis = ""

            diagnostics = alert.get("diagnostics", {})
            error_logs = "\n".join(diagnostics.get("last_logs", []))

            async with self.docs_client:
                result = await self.docs_client.call_tool(
                    "create_incident_doc",
                    {
                        "incident_id": classification.incident_id,
                        "severity": classification.severity,
                        "service": classification.service,
                        "description": classification.description,
                        "likely_cause": classification.likely_cause,
                        "suggested_action": classification.suggested_action,
                        "affected_users": classification.affected_users,
                        "region": classification.region,
                        "error_rate": classification.error_rate,
                        "latency_p99_ms": str(alert.get("latency_p99_ms", "N/A")),
                        "requests_per_minute": str(alert.get("requests_per_minute", "N/A")),
                        "deployment_id": classification.deployment_id,
                        "deployment_age_minutes": str(diagnostics.get("deployment_age_minutes", "unknown")),
                        "cpu_usage": diagnostics.get("cpu_usage", "N/A"),
                        "memory_usage": diagnostics.get("memory_usage", "N/A"),
                        "error_logs": error_logs,
                        "similar_incidents": similar_text,
                        "analysis": analysis,
                    },
                    timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
            payload = _result_to_dict(result)
            log.info(f"[{self.session_id}] Created doc via MCP: {payload}")
            return {
                "doc_url": payload.get("doc_url", f"https://docs.google.com/document/d/{classification.incident_id}"),
                "success": payload.get("success", False),
            }
        except Exception as e:
            log.error(f"[{self.session_id}] DocsAgent MCP call failed: {e}")
            return {"success": False, "error": str(e)}

    async def _block_calendar(self, classification: IncidentClassification) -> dict:
        """Block calendar and create Meet for P0 incident"""
        try:
            async with self.calendar_client:
                block_result = await self.calendar_client.call_tool(
                    "block_oncall_calendar",
                    {
                        "incident_id": classification.incident_id,
                        "service": classification.service,
                        "severity": classification.severity,
                        "duration_minutes": 120,
                    },
                    timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
                meet_result = await self.calendar_client.call_tool(
                    "create_meet_link",
                    {
                        "incident_id": classification.incident_id,
                        "service": classification.service,
                        "severity": classification.severity,
                    },
                    timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
            block_payload = _result_to_dict(block_result)
            meet_payload = _result_to_dict(meet_result)
            log.info(f"[{self.session_id}] Blocked calendar via MCP: {block_payload}")
            log.info(f"[{self.session_id}] Created Meet via MCP: {meet_payload}")
            return {
                "meet_url": meet_payload.get("meet_url", f"https://meet.google.com/lookup/{classification.incident_id}"),
                "success": meet_payload.get("success", False),
            }
        except Exception as e:
            log.error(f"[{self.session_id}] CalendarAgent MCP call failed: {e}")
            return {"success": False, "error": str(e)}


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


def _result_to_dict(result) -> dict:
    if isinstance(getattr(result, "data", None), dict):
        return result.data
    if isinstance(getattr(result, "structured_content", None), dict):
        return result.structured_content
    return {
        "success": not getattr(result, "is_error", True),
        "content": getattr(result, "content", []),
    }


_orchestrator = IncidentOrchestrator()


async def process_incident_alert(alert: dict) -> dict:
    """Public API called by webhook"""
    return await _orchestrator.process_alert(alert)


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
