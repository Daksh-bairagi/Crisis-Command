"""
Post-mortem generation.

After an incident is resolved, this module generates a structured post-mortem
and appends it to the incident's Google Doc. LLM is appropriate here because
the task requires synthesizing structured data into narrative analysis —
something deterministic logic cannot do.
"""

import os
import sys
import asyncio

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
from database.db import get_incident
from orchestrator.mcp_clients import create_mcp_client
from logger import get_logger

log = get_logger("post_mortem")


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


EXTERNAL_ACTIONS_ENABLED = _env_flag("ENABLE_EXTERNAL_ACTIONS", bool(os.getenv("K_SERVICE")))
GENAI_FEATURES_ENABLED = _env_flag("ENABLE_GENAI_FEATURES", bool(os.getenv("K_SERVICE")))
EXTERNAL_CALL_TIMEOUT_SECONDS = float(os.getenv("EXTERNAL_CALL_TIMEOUT_SECONDS", "8"))


def _extract_doc_id(doc_url: str) -> str | None:
    """Extract Google Doc ID from a URL like https://docs.google.com/document/d/{ID}/edit"""
    try:
        parts = doc_url.split("/d/")
        if len(parts) >= 2:
            return parts[1].split("/")[0]
    except Exception:
        pass
    return None


async def _call_gemini(prompt: str) -> str:
    """Call Gemini to generate post-mortem text. Returns empty string on failure."""
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            log.error("GOOGLE_API_KEY not set — cannot generate post-mortem")
            return ""
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, prompt),
            timeout=30.0,
        )
        return response.text.strip()
    except Exception as e:
        log.error("Gemini call failed during post-mortem generation: %s", e)
        return ""


async def generate_post_mortem(incident_id: str) -> bool:
    """
    Fetch the resolved incident, generate a post-mortem via Gemini,
    and append it to the incident Doc via the Docs MCP tool.

    Called as a background task after an incident is marked resolved.
    Returns True if the post-mortem was successfully generated and written,
    False otherwise. Failures are logged and do not propagate.
    """
    try:
        # 1. Fetch incident record
        incident = await get_incident(incident_id)
        if not incident:
            log.error("Post-mortem: incident %s not found in DB", incident_id)
            return False

        doc_url = incident.get("doc_url")
        if not doc_url:
            log.warning("Post-mortem: no doc_url for %s — skipping", incident_id)
            return False

        doc_id = _extract_doc_id(doc_url)
        if not doc_id:
            log.error("Post-mortem: could not extract doc_id from %s", doc_url)
            return False

        # 2. Generate post-mortem text
        if not GENAI_FEATURES_ENABLED:
            log.info("Post-mortem: GenAI disabled — skipping for %s", incident_id)
            return False

        mttr = incident.get("mttr_seconds") or 0
        mttr_display = f"{mttr // 60}m {mttr % 60}s" if mttr else "not yet recorded"

        resolution_notes = incident.get("resolution_notes") or ""
        resolution_line = (
            f"- Actual Resolution Applied: {resolution_notes}"
            if resolution_notes
            else "- Actual Resolution: (not recorded — engineer can add via Chat: resolution <id> <what was done>)"
        )

        prompt = f"""You are an SRE writing a post-mortem for a resolved production incident.

Incident details:
- ID: {incident['incident_id']}
- Service: {incident['service']}
- Severity: {incident['severity']}
- Description: {incident['description']}
- Root Cause (initial classification): {incident['likely_cause']}
- Suggested Action (initial): {incident['suggested_action']}
{resolution_line}
- Affected Users: {incident['affected_users']}
- Region: {incident.get('region', 'unknown')}
- MTTR: {mttr_display}

Write a concise, honest post-mortem with these sections:

## Summary
2-3 sentences. Non-technical. What happened and what was the impact.

## Root Cause
Specific and technical. Not "a bug was introduced" — say what kind of bug, where.

## Impact
Users affected, duration, scope. Quantify where possible.

## What Went Well
Be specific. What did the team or system do right during response?

## What Could Be Improved
Be honest. What slowed down detection or resolution?

## Action Items
3-5 items. Each must be specific, assignable, and have a timeframe.
Format: - [Action] — DRI: [Team/Role] — Due: [timeframe]
Avoid vague items like "improve monitoring". Say what monitoring, on what metric, with what threshold.

Plain text with markdown headers. No preamble."""

        post_mortem_text = await _call_gemini(prompt)
        if not post_mortem_text:
            return False

        log.info("Post-mortem generated for %s (%d chars)", incident_id, len(post_mortem_text))

        # 3. Append to incident doc via Docs MCP tool
        if not EXTERNAL_ACTIONS_ENABLED:
            log.info("Post-mortem (external actions disabled, printing only):\n%s", post_mortem_text)
            return True

        docs_client = create_mcp_client("agents/docs_agent/mcp_server.py", "docs-mcp")
        async with docs_client:
            await docs_client.call_tool(
                "update_doc_section",
                {
                    "doc_id": doc_id,
                    "section_name": "POST-MORTEM",
                    "content": post_mortem_text,
                },
                timeout=EXTERNAL_CALL_TIMEOUT_SECONDS,
            )

        log.info("Post-mortem written to doc for %s", incident_id)
        return True

    except Exception as e:
        log.error("Post-mortem generation failed for %s: %s", incident_id, e)
        return False
