from fastmcp import FastMCP
from googleapiclient.discovery import build
import sys
import os 
from datetime import datetime
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)
from logger import get_logger
from auth.oauth import get_credentials
log= get_logger("chat_mcp_server")
load_dotenv(os.path.join(ROOT_DIR, ".env"))
mcp= FastMCP("chat-server")

SPACE_ID = os.getenv("CHAT_SPACE_ID")


def get_service():
    creds = get_credentials()
    return build('chat', 'v1', credentials=creds)

@mcp.tool()
def post_text_message(text: str) -> dict:
    """Post a text message to the Google Chat space."""
    try:
        service = get_service()
        result = service.spaces().messages().create(
            parent=SPACE_ID,
            body={"text": text}
        ).execute()
        log.info(f"Posted message to chat with id: {result['name']}")
        return {"success": True, "message_name": result.get("name")}
    except Exception as e:
        log.error(f"Error posting message to chat: {e}")
        return {"success": False, "error": str(e)}
    

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
    similar_incident: str = ""
) -> dict:
    """
    Post a rich incident alert to Chat space.
    Formats all incident details clearly for the SRE team.
    Uses text-based format (no Cards) to avoid IIITG Gmail restrictions.
    """
    try:
        chat_service = get_service()
        emoji = {"P0": "🔴", "P1": "🟡", "P2": "🟢"}.get(severity, "⚪")
        detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"{emoji} *{severity} INCIDENT — {incident_id}*",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"*Service:* {service}",
            f"*Region:* {region}",
            f"*Detected:* {detected_at}",
            f"",
            f"*Description:*",
            f"{description}",
            f"",
            f"*Affected Users:* {affected_users}",
            f"",
            f"*Root Cause Analysis:*",
            f"{likely_cause}",
            f"",
            f"*Suggested Action:*",
            f"{suggested_action}",
        ]

        if similar_incidents:
            lines.append(f"")
            lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"⚡ *Similar Past Incidents Found:*")
            lines.append(f"{similar_incidents}")

        if doc_link:
            lines.append(f"")
            lines.append(f"📄 *Incident Document:*")
            lines.append(f"{doc_link}")

        if meet_link:
            lines.append(f"")
            lines.append(f"📹 *War Room (Google Meet):*")
            lines.append(f"{meet_link}")

        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"*Actions:*")
        lines.append(f"Reply: `acknowledge {incident_id}` to acknowledge")
        lines.append(f"Reply: `resolve {incident_id}` to mark resolved")

        message = "\n".join(lines)

        result = chat_service.spaces().messages().create(
            parent=SPACE_ID,
            body={"text": message}
        ).execute()

        log.info("Incident alert posted: %s", result.get("name"))
        return {
            "success": True,
            "message_name": result.get("name"),
            "incident_id": incident_id
        }

    except Exception as e:
        log.error("Failed to post alert: %s", str(e))
        return {"success": False, "error": str(e)}

@mcp.tool()
def update_message(message_name: str, new_text: str) -> dict:
    """
    Update an existing Chat message.
    Use after acknowledge or resolve to reflect new status.
    message_name format: spaces/XXX/messages/YYY
    """
    try:
        service = get_service()
        result = service.spaces().messages().update(
            name=message_name,
            updateMask="text",
            body={"text": new_text}
        ).execute()
        log.info("Message updated: %s", message_name)
        return {"success": True, "message_name": result.get("name")}
    except Exception as e:
        log.error("Failed to update message: %s", str(e))
        return {"success": False, "error": str(e)}


@mcp.tool()
def post_status_update(incident_id: str, status: str, actor: str = "System") -> dict:
    """
    Post a quick status update after acknowledge/resolve.
    """
    try:
        service = get_service()
        status_emoji = {"acknowledged": "✅", "resolved": "🎉"}.get(status, "ℹ️")
        
        message = f"{status_emoji} *Incident {incident_id} {status}* by {actor}"
        
        result = service.spaces().messages().create(
            parent=SPACE_ID,
            body={"text": message}
        ).execute()
        
        log.info(f"Status update posted: {incident_id} - {status}")
        return {"success": True, "message_name": result.get("name")}
    except Exception as e:
        log.error(f"Failed to post status update: {e}")
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    log.info("Starting Chat MCP server on stdio transport")
    mcp.run()
