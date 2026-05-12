from fastmcp import FastMCP
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from agents.chat_agent import actions
from logger import get_mcp_logger

log = get_mcp_logger("chat_mcp_server")
mcp = FastMCP("chat-server")


@mcp.tool()
def post_text_message(text: str) -> dict:
    """Post a text message to the Google Chat space."""
    return actions.post_text_message(text)


@mcp.tool()
def post_incident_alert(
    incident_id: str,
    severity: str,
    service: str,
    description: str,
    likely_cause: str,
    suggested_action: str,
    affected_users: str,
    region: str = "unknown",
    doc_link: str = "",
    meet_link: str = "",
    similar_incidents: str = "",
) -> dict:
    """
    Post a rich incident alert to Chat space.
    Formats all incident details clearly for the SRE team.
    Uses text-based format (no Cards) to avoid IIITG Gmail restrictions.
    """
    return actions.post_incident_alert(
        incident_id=incident_id,
        severity=severity,
        service=service,
        description=description,
        likely_cause=likely_cause,
        suggested_action=suggested_action,
        affected_users=affected_users,
        region=region,
        doc_link=doc_link,
        meet_link=meet_link,
        similar_incidents=similar_incidents,
    )


@mcp.tool()
def update_message(message_name: str, new_text: str) -> dict:
    """
    Update an existing Chat message.
    Use after acknowledge or resolve to reflect new status.
    message_name format: spaces/XXX/messages/YYY
    """
    return actions.update_message(message_name, new_text)


@mcp.tool()
def post_status_update(incident_id: str, status: str, actor: str = "System") -> dict:
    """
    Post a quick status update after acknowledge/resolve.
    """
    return actions.post_status_update(incident_id, status, actor)


if __name__ == "__main__":
    log.info("chat MCP server starting on stdio transport")
    mcp.run()
