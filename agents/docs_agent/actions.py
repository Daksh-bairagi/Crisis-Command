from googleapiclient.discovery import build
import sys
import os
from datetime import datetime
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from logger import get_logger
from auth.oauth import get_credentials

log = get_logger("docs_actions")
load_dotenv(os.path.join(ROOT_DIR, ".env"))

DOCS_FOLDER_ID = os.getenv("DOCS_FOLDER_ID", "root")  # Default to Drive root


def _get_docs_service():
    """Build Google Docs API service"""
    creds = get_credentials()
    return build('docs', 'v1', credentials=creds)


def _get_drive_service():
    """Build Google Drive API service for folder operations"""
    creds = get_credentials()
    return build('drive', 'v3', credentials=creds)


def create_incident_doc(
    incident_id: str,
    severity: str,
    service: str,
    description: str,
    likely_cause: str,
    suggested_action: str,
    affected_users: str,
    region: str = "unknown",
    error_rate: str = "unknown",
    latency_p99_ms: str = "N/A",
    requests_per_minute: str = "N/A",
    deployment_id: str = "unknown",
    deployment_age_minutes: str = "unknown",
    cpu_usage: str = "N/A",
    memory_usage: str = "N/A",
    error_logs: str = "",
    similar_incidents: str = "",
    analysis: str = ""
) -> dict:
    """
    Create a new Google Doc for incident response with full diagnostic context.

    Returns:
        {
            "success": bool,
            "doc_url": str,
            "doc_id": str,
            "error": str (if failed)
        }
    """
    try:
        log.info(f"Creating incident doc for {incident_id}")

        docs_service = _get_docs_service()
        doc = docs_service.documents().create(body={
            "title": f"[{severity}] {service} Incident - {incident_id}"
        }).execute()
        doc_id = doc["documentId"]

        detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

        error_rate_pct = "unknown"
        try:
            error_rate_pct = f"{float(error_rate) * 100:.1f}%"
        except (TypeError, ValueError):
            pass

        logs_section = error_logs.strip() if error_logs.strip() else "(no log data captured)"
        similar_section = similar_incidents.strip() if similar_incidents.strip() else "(no similar past incidents found)"
        analysis_section = analysis.strip() if analysis.strip() else "(analysis unavailable)"

        dep_label = f"{deployment_id}"
        if deployment_age_minutes not in ("unknown", "N/A", ""):
            dep_label += f" — deployed {deployment_age_minutes} minutes before incident"

        doc_content = f"""INCIDENT RESPONSE DOCUMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Incident ID:  {incident_id}
Severity:     {severity}
Status:       ACTIVE
Service:      {service}
Region:       {region}
Detected At:  {detected_at}

━━━ IMPACT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Description:      {description}
Affected Users:   {affected_users}
Error Rate:       {error_rate_pct}
P99 Latency:      {latency_p99_ms}ms
Requests/min:     {requests_per_minute}

━━━ DEPLOYMENT CONTEXT ━━━━━━━━━━━━━━━━━━━━━━━━━━

Last Deployment:  {dep_label}

━━━ DIAGNOSTIC SIGNALS ━━━━━━━━━━━━━━━━━━━━━━━━━━

CPU Usage:     {cpu_usage}
Memory Usage:  {memory_usage}

Error Logs:
{logs_section}

━━━ INCIDENT ANALYSIS (AI-generated) ━━━━━━━━━━━━

{analysis_section}

━━━ SIMILAR PAST INCIDENTS ━━━━━━━━━━━━━━━━━━━━━━

{similar_section}

━━━ TIMELINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{detected_at}  — Incident detected, {incident_id} opened
[ACKNOWLEDGED_TIME]
[RESOLVED_TIME]

━━━ RESOLUTION NOTES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[What was done to resolve — add via Chat: resolution {incident_id} <what you did>]

━━━ POST-MORTEM ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[POST-MORTEM]
"""

        requests = [
            {
                "insertText": {
                    "text": doc_content,
                    "location": {"index": 1},
                }
            }
        ]

        # Apply formatting
        batch_update = {"requests": requests}
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body=batch_update
        ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

        # Move to incidents folder if specified
        if DOCS_FOLDER_ID and DOCS_FOLDER_ID != "root":
            try:
                drive_service = _get_drive_service()
                drive_service.files().update(
                    fileId=doc_id,
                    addParents=DOCS_FOLDER_ID,
                    removeParents="root"
                ).execute()
            except Exception as e:
                log.warning(f"Could not move doc to folder: {e}")

        log.info(f"Created incident doc: {doc_url}")
        return {
            "success": True,
            "doc_url": doc_url,
            "doc_id": doc_id
        }

    except Exception as e:
        log.error(f"Error creating incident doc: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def update_doc_section(
    doc_id: str,
    section_name: str,
    content: str
) -> dict:
    """
    Update a specific section of an existing incident doc.

    Args:
        doc_id: Google Doc ID
        section_name: Section to update (e.g., "SIMILAR_PAST_INCIDENTS", "TIMELINE")
        content: New content for that section

    Returns:
        {
            "success": bool,
            "error": str (if failed)
        }
    """
    try:
        log.info(f"Updating section {section_name} in doc {doc_id}")

        docs_service = _get_docs_service()
        doc = docs_service.documents().get(documentId=doc_id).execute()

        # Find section marker and update
        # Simple implementation: search for section header and replace next content
        # In production, you'd parse the document structure more carefully

        section_marker = f"[{section_name}]" if not section_name.startswith("[") else section_name

        # Get current content to find the position
        content_text = ""
        for element in doc.get("body", {}).get("content", []):
            if "paragraph" in element:
                for run in element["paragraph"].get("elements", []):
                    if "textRun" in run:
                        content_text += run["textRun"].get("content", "")

        if section_marker in content_text:
            # Find and replace
            requests = [
                {
                    "replaceAllText": {
                        "containsText": {"text": section_marker, "matchCase": False},
                        "replaceText": f"{section_marker}\n{content}"
                    }
                }
            ]

            batch_update = {"requests": requests}
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body=batch_update
            ).execute()

            log.info(f"Updated section {section_name}")
            return {"success": True}
        else:
            # Section not found, append instead
            requests = [
                {
                    "insertText": {
                        "text": f"\n{content}",
                        "location": {"index": len(content_text)}
                    }
                }
            ]

            batch_update = {"requests": requests}
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body=batch_update
            ).execute()

            log.info(f"Appended to doc (section {section_name} not found)")
            return {"success": True}

    except Exception as e:
        log.error(f"Error updating doc section: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def add_similar_incident(
    doc_id: str,
    similar_incident_id: str,
    similar_incident_content: str
) -> dict:
    """
    Inject similar past incident information into the doc.
    This is called by the RAG system after finding similar incidents.
    """
    try:
        log.info(f"Adding similar incident {similar_incident_id} to doc {doc_id}")

        content = f"""
📌 Similar Past Incident: {similar_incident_id}
{similar_incident_content}
"""

        return update_doc_section(
            doc_id,
            "SIMILAR_PAST_INCIDENTS",
            content
        )

    except Exception as e:
        log.error(f"Error adding similar incident: {e}")
        return {
            "success": False,
            "error": str(e)
        }
