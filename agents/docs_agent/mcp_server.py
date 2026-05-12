from fastmcp import FastMCP
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from agents.docs_agent import actions
from logger import get_mcp_logger

log = get_mcp_logger("docs_mcp_server")
mcp = FastMCP("docs-server")


@mcp.tool()
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
    return actions.create_incident_doc(
        incident_id=incident_id,
        severity=severity,
        service=service,
        description=description,
        likely_cause=likely_cause,
        suggested_action=suggested_action,
        affected_users=affected_users,
        region=region,
        error_rate=error_rate,
        latency_p99_ms=latency_p99_ms,
        requests_per_minute=requests_per_minute,
        deployment_id=deployment_id,
        deployment_age_minutes=deployment_age_minutes,
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        error_logs=error_logs,
        similar_incidents=similar_incidents,
        analysis=analysis,
    )


@mcp.tool()
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
    return actions.update_doc_section(doc_id, section_name, content)


@mcp.tool()
def add_similar_incident(
    doc_id: str,
    similar_incident_id: str,
    similar_incident_content: str
) -> dict:
    """
    Inject similar past incident information into the doc.
    This is called by the RAG system after finding similar incidents.
    """
    return actions.add_similar_incident(doc_id, similar_incident_id, similar_incident_content)


if __name__ == "__main__":
    log.info("docs MCP server starting on stdio transport")
    mcp.run()
