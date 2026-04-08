from fastmcp import FastMCP
from googleapiclient.discovery import build
from google.api_core import retry
import sys
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from logger import get_logger
from auth.oauth import get_credentials

log = get_logger("calendar_mcp_server")
load_dotenv(os.path.join(ROOT_DIR, ".env"))

mcp = FastMCP("calendar-server")

ONCALL_EMAIL = os.getenv("ONCALL_EMAIL", "oncall@example.com")


def get_calendar_service():
    """Build Google Calendar API service"""
    creds = get_credentials()
    return build('calendar', 'v3', credentials=creds)


def get_meet_service():
    """Build Google Meet API service (via Calendar events)"""
    creds = get_credentials()
    return build('calendar', 'v3', credentials=creds)


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
    try:
        log.info(f"Blocking calendar for {ONCALL_EMAIL}: {incident_id}")
        
        calendar_service = get_calendar_service()
        
        # Create calendar event
        now = datetime.utcnow()
        end_time = now + timedelta(minutes=duration_minutes)
        
        event = {
            "summary": f"WAR ROOM: [{severity}] {service} - {incident_id}",
            "description": f"Incident Response War Room\nIncident ID: {incident_id}\nService: {service}",
            "start": {
                "dateTime": now.isoformat() + "Z",
                "timeZone": "UTC"
            },
            "end": {
                "dateTime": end_time.isoformat() + "Z",
                "timeZone": "UTC"
            },
            "attendees": [
                {"email": ONCALL_EMAIL, "responseStatus": "accepted"}
            ],
            "transparency": "opaque",  # Mark as busy
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 0}  # Immediate notification
                ]
            }
        }
        
        created_event = calendar_service.events().insert(
            calendarId=ONCALL_EMAIL,
            body=event,
            conferenceDataVersion=1
        ).execute()
        
        event_id = created_event['id']
        event_url = created_event.get('htmlLink', '')
        
        log.info(f"Calendar event created: {event_id}")
        
        return {
            "success": True,
            "event_id": event_id,
            "event_url": event_url
        }
        
    except Exception as e:
        log.error(f"Error blocking calendar: {e}")
        return {
            "success": False,
            "error": str(e)
        }


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
    try:
        log.info(f"Creating Meet link for incident {incident_id}")
        
        calendar_service = get_calendar_service()
        
        # Create calendar event with Meet link
        now = datetime.utcnow()
        end_time = now + timedelta(hours=2)
        
        event = {
            "summary": f"WAR ROOM: [{severity}] {service} Incident",
            "description": f"""
Incident Response War Room
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Incident ID: {incident_id}
Service: {service}
Severity: {severity}

Join the war room to coordinate incident response.
""",
            "start": {
                "dateTime": now.isoformat() + "Z",
                "timeZone": "UTC"
            },
            "end": {
                "dateTime": end_time.isoformat() + "Z",
                "timeZone": "UTC"
            },
            "conferenceData": {
                "createRequest": {
                    "requestId": f"war-room-{incident_id}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"}
                }
            },
            "attendees": [
                {"email": ONCALL_EMAIL}
            ]
        }
        
        created_event = calendar_service.events().insert(
            calendarId=ONCALL_EMAIL,
            body=event,
            conferenceDataVersion=1
        ).execute()
        
        event_id = created_event['id']
        
        # Extract Meet link from conference data
        meet_url = None
        if "conferenceData" in created_event:
            entry_points = created_event["conferenceData"].get("entryPoints", [])
            for entry in entry_points:
                if entry.get("entryPointType") == "video":
                    meet_url = entry.get("uri")
                    break
        
        if not meet_url:
            # Fallback: construct from event ID
            meet_url = f"https://meet.google.com/lookup/{event_id}"
        
        log.info(f"Meet link created: {meet_url}")
        
        return {
            "success": True,
            "meet_url": meet_url,
            "event_id": event_id
        }
        
    except Exception as e:
        log.error(f"Error creating Meet link: {e}")
        return {
            "success": False,
            "error": str(e)
        }


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
    try:
        log.info(f"Adding {attendee_email} to event {event_id}")
        
        calendar_service = get_calendar_service()
        
        event = calendar_service.events().get(
            calendarId=ONCALL_EMAIL,
            eventId=event_id
        ).execute()
        
        # Check if already attending
        existing_attendees = [a["email"] for a in event.get("attendees", [])]
        if attendee_email not in existing_attendees:
            event["attendees"].append({"email": attendee_email})
            
            calendar_service.events().update(
                calendarId=ONCALL_EMAIL,
                eventId=event_id,
                body=event
            ).execute()
        
        log.info(f"Attendee {attendee_email} added to event")
        return {"success": True}
        
    except Exception as e:
        log.error(f"Error adding attendee: {e}")
        return {
            "success": False,
            "error": str(e)
        }


if __name__ == "__main__":
    log.info("Calendar MCP server starting on stdio transport")
    mcp.run()
