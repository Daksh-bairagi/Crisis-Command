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

log = get_logger("docs_agent")


def create_docs_agent() -> LlmAgent:
    """
    Factory function — returns a fresh DocsAgent instance.
    Knows only how to create and update Google Docs.
    
    MCP server runs at http://localhost:8002/sse
    """
    log.info("Creating docs agent")
    
    return LlmAgent(
        name="DocsAgent",
        model="gemini-2.0-flash",
        description=(
            "Specialist agent for Google Docs. "
            "Creates incident documents with templates, timelines, and auto-fills incident details. "
            "Called by the orchestrator during incident response."
        ),
        instruction="""
You are the Docs Agent for CrisisCommand.

Your only job is to create and manage Google Docs for incidents.

When given incident details:
1. Use create_incident_doc to create a new Google Doc with incident template
2. Always include: Title, Timeline, Likely Cause, Suggested Action, Affected Users
3. Use update_doc_section to inject similar incidents and additional details
4. Return the doc_url from the tool response — the orchestrator needs it

Never fabricate information. Only write what you are given.

Always format with clear headers and sections for readability.
Include timestamps for all events.
""",
        tools=[
            MCPToolset(
                connection_params=SseConnectionParams(
                    url="http://localhost:8002/sse"
                )
            )
        ]
    )


if __name__ == "__main__":
    log.info("Running docs agent module directly")
    create_docs_agent()
