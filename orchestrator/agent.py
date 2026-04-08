"""
CrisisCommand Orchestrator - Multi-Agent Incident Response

Main coordinator that orchestrates incident response by calling MCP-based tools directly.
"""

import os
import sys
import asyncio
from datetime import datetime
import json
import importlib.util

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()

from logger import get_logger
from orchestrator.classifier import classify, IncidentClassification
from database.db import (
    store_incident, 
    store_incident_memory,
    search_similar_incidents,
    log_trace
)

import google.generativeai as genai

log = get_logger("orchestrator")
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))


# ─── MAIN ORCHESTRATOR AGENT ────────────────────────────────────

class IncidentOrchestrator:
    """Main orchestrator coordinating incident response"""

    def __init__(self):
        self.session_id = None

    async def process_alert(self, alert: dict) -> dict:
        """Main orchestration flow"""
        import uuid
        self.session_id = f"session-{str(uuid.uuid4())[:8]}"
        
        try:
            log.info(f"[{self.session_id}] ▶️  Orchestrating: {alert.get('service')}")
            
            # Step 1: Classify
            log.info(f"[{self.session_id}] Step 1: Classifying alert...")
            classification = await classify(alert)
            log.info(f"[{self.session_id}] → {classification.severity}: {classification.likely_cause}")
            
            # Step 2: Search for similar incidents
            log.info(f"[{self.session_id}] Step 2: Searching similar incidents...")
            similar = await self._search_similar_incidents(alert)
            similar_text = similar[0]["content"] if similar else ""
            
            # Step 3: Store incident
            log.info(f"[{self.session_id}] Step 3: Storing incident...")
            if not await store_incident(classification):
                raise Exception("Failed to store incident")
            
            # Step 4: Create memory for RAG
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
            
            # Invoke Chat Agent
            if classification.activate_chat:
                log.info(f"[{self.session_id}] → Invoking ChatAgent...")
                chat_result = await self._post_to_chat(classification, similar_text)
                response["chat_message"] = chat_result.get("message_id")
            
            # Invoke Docs Agent
            if classification.activate_docs:
                log.info(f"[{self.session_id}] → Invoking DocsAgent...")
                docs_result = await self._create_doc(classification, similar_text)
                response["doc_url"] = docs_result.get("doc_url")
            
            # Invoke Calendar Agent
            if classification.activate_calendar:
                log.info(f"[{self.session_id}] → Invoking CalendarAgent...")
                cal_result = await self._block_calendar(classification)
                response["meet_url"] = cal_result.get("meet_url")
                
                # Post Meet link to Chat
                if cal_result.get("success") and cal_result.get("meet_url"):
                    await self._post_meet_to_chat(classification, cal_result.get("meet_url"))
            
            # Log trace
            await log_trace(self.session_id, "orchestrator", "process_alert", 
                          input_data={"alert": alert}, output_data=response)
            
            log.info(f"[{self.session_id}] ✅ Done: {classification.incident_id}")
            return response
            
        except Exception as e:
            log.error(f"[{self.session_id}] ❌ Failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _search_similar_incidents(self, alert: dict) -> list[dict]:
        """Generate embedding using gemini-embedding-001"""
        try:
            response = genai.embed_content(
                model="models/gemini-embedding-001",
                content=f"{alert.get('service')}: {alert.get('description')}",
                task_type="RETRIEVAL_DOCUMENT"
            )
            embedding = response['embedding']
            results = await search_similar_incidents(embedding, limit=3)
            return results
        except Exception as e:
            log.error(f"[{self.session_id}] Embedding failed: {e}")
            return []

    async def _create_incident_memory(self, classification: IncidentClassification):
        """Store incident as memory for future RAG"""
        try:
            memory_text = f"[{classification.severity}] {classification.service}: {classification.description} - {classification.likely_cause}"
            response = genai.embed_content(
                model="models/gemini-embedding-001",
                content=memory_text,
                task_type="RETRIEVAL_DOCUMENT"
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
            # Import chat MCP tools dynamically
            import importlib.util
            spec = importlib.util.spec_from_file_location("chat_mcp", os.path.join(ROOT_DIR, "agents", "chat_agent", "mcp_server.py"))
            chat_mcp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(chat_mcp)
            
            result = chat_mcp.post_incident_alert(
                incident_id=classification.incident_id,
                severity=classification.severity,
                service=classification.service,
                description=classification.description,
                likely_cause=classification.likely_cause,
                suggested_action=classification.suggested_action,
                affected_users=classification.affected_users,
                doc_link="",
                meet_link="",
                similar_incidents=similar_text or ""
            )
            log.info(f"[{self.session_id}] Posted to Google Chat: {result}")
            return {"message_id": result.get("message_name", f"msg-{classification.incident_id}"), "success": result.get("success")}
        except Exception as e:
            log.error(f"[{self.session_id}] ChatAgent failed: {e}")
            return {"success": False, "error": str(e)}

    async def _post_meet_to_chat(self, classification: IncidentClassification, meet_url: str) -> dict:
        """Post Meet link to Google Chat"""
        try:
            # Import chat MCP tools dynamically
            import importlib.util
            spec = importlib.util.spec_from_file_location("chat_mcp", os.path.join(ROOT_DIR, "agents", "chat_agent", "mcp_server.py"))
            chat_mcp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(chat_mcp)
            
            message = f"🔗 **WAR ROOM**: {meet_url}\n\nJoin the Google Meet to coordinate response for [{classification.severity}] {classification.service} incident."
            result = chat_mcp.post_text_message(text=message)
            log.info(f"[{self.session_id}] Posted Meet link to Chat: {result}")
            return result
        except Exception as e:
            log.error(f"[{self.session_id}] Failed to post Meet to Chat: {e}")
            return {"success": False, "error": str(e)}

    async def _create_doc(self, classification: IncidentClassification, similar_text: str) -> dict:
        """Create incident document in Google Docs"""
        try:
            # Import docs MCP tools dynamically
            import importlib.util
            spec = importlib.util.spec_from_file_location("docs_mcp", os.path.join(ROOT_DIR, "agents", "docs_agent", "mcp_server.py"))
            docs_mcp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(docs_mcp)
            
            result = docs_mcp.create_incident_doc(
                incident_id=classification.incident_id,
                severity=classification.severity,
                service=classification.service,
                description=classification.description,
                likely_cause=classification.likely_cause,
                suggested_action=classification.suggested_action,
                affected_users=classification.affected_users
            )
            log.info(f"[{self.session_id}] Created doc: {result}")
            return {"doc_url": result.get("doc_url", f"https://docs.google.com/document/d/{classification.incident_id}"), "success": result.get("success")}
        except Exception as e:
            log.error(f"[{self.session_id}] DocsAgent failed: {e}")
            return {"success": False, "error": str(e)}

    async def _block_calendar(self, classification: IncidentClassification) -> dict:
        """Block calendar and create Meet for P0 incident"""
        try:
            # Import calendar MCP tools dynamically
            import importlib.util
            spec = importlib.util.spec_from_file_location("calendar_mcp", os.path.join(ROOT_DIR, "agents", "calendar_agent", "mcp_server.py"))
            calendar_mcp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(calendar_mcp)
            
            # Block calendar
            block_result = calendar_mcp.block_oncall_calendar(
                incident_id=classification.incident_id,
                service=classification.service,
                severity=classification.severity,
                duration_minutes=120
            )
            log.info(f"[{self.session_id}] Blocked calendar: {block_result}")
            
            # Create Meet link
            meet_result = calendar_mcp.create_meet_link(
                incident_id=classification.incident_id,
                service=classification.service,
                severity=classification.severity
            )
            log.info(f"[{self.session_id}] Created Meet: {meet_result}")
            return {"meet_url": meet_result.get("meet_url", f"https://meet.google.com/lookup/{classification.incident_id}"), "success": meet_result.get("success")}
        except Exception as e:
            log.error(f"[{self.session_id}] CalendarAgent failed: {e}")
            return {"success": False, "error": str(e)}


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
