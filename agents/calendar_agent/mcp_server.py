from fastmcp import FastMCP
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from agents.calendar_agent import actions
from logger import get_mcp_logger

log = get_mcp_logger("calendar_mcp_server")
mcp = FastMCP("calendar-server")


@mcp.tool()
def block_oncall_calendar(
    incident_id: str,
    service: str,
    severity: str,
    duration_minutes: int = 120
) -> dict:
    """
    Block the on-call engineer's calendar for a P0/P1 incident.

    Args:
        incident_id: Incident ID (e.g., INC-ABC123)
        service: Service name
        severity: P0, P1, or P2
        duration_minutes: How long to block (default 2 hours)

    Returns:
        {
            "success": bool,
            "event_id": str,
            "event_url": str,
            "error": str (if failed)
        }
    """
    return actions.block_oncall_calendar(incident_id, service, severity, duration_minutes)


@mcp.tool()
def create_meet_link(
    incident_id: str,
    service: str,
    severity: str
) -> dict:
    """
    Create a Google Meet link for the incident war room.
    This creates a Calendar event with an attached Meet link.

    Args:
        incident_id: Incident ID
        service: Service name
        severity: P0, P1, or P2

    Returns:
        {
            "success": bool,
            "meet_url": str,
            "event_id": str,
            "error": str (if failed)
        }
    """
    return actions.create_meet_link(incident_id, service, severity)


@mcp.tool()
def add_attendee_to_event(
    event_id: str,
    attendee_email: str
) -> dict:
    """
    Add an attendee to the war room calendar event.

    Args:
        event_id: Calendar event ID
        attendee_email: Email to add

    Returns:
        {
            "success": bool,
            "error": str (if failed)
        }
    """
    return actions.add_attendee_to_event(event_id, attendee_email)


if __name__ == "__main__":
    log.info("calendar MCP server starting on stdio transport")
    mcp.run()
