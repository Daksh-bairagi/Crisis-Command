import os
import sys
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

load_dotenv()

from logger import get_logger
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset, SseConnectionParams

log = get_logger("calendar_agent")


def create_calendar_agent() -> LlmAgent:
    """
    Factory function — returns a fresh CalendarAgent instance.
    Knows only how to manage Google Calendar and create Meet links.
    
    MCP server runs at http://localhost:8003/sse
    """
    log.info("Creating calendar agent")
    
    return LlmAgent(
        name="CalendarAgent",
        model="gemini-2.0-flash",
        description=(
            "Specialist agent for Google Calendar and Meet. "
            "Blocks on-call engineer's calendar and creates war room Meet links. "
            "Called by the orchestrator for P0 incidents."
        ),
        instruction="""
You are the Calendar Agent for CrisisCommand.

Your only job is to block calendars and create Meet links for war rooms.

When processing a P0 incident:
1. Use block_oncall_calendar to reserve 2 hours for the on-call engineer
2. Use create_meet_link to create the war room (meeting title should include incident ID)
3. Return both calendar_event_id and meet_url
4. The orchestrator will post these links to Chat and Docs

Never fabricate information. Only use the tools provided.
Always set calendar events with a clear title like "WAR ROOM: [Service] Incident".
""",
        tools=[
            MCPToolset(
                connection_params=SseConnectionParams(
                    url="http://localhost:8003/sse"
                )
            )
        ]
    )


if __name__ == "__main__":
    log.info("Running calendar agent module directly")
    create_calendar_agent()
