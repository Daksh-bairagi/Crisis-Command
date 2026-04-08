import os
import sys
from dotenv import load_dotenv
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)
load_dotenv()
from logger import get_logger

log = get_logger("chat_agent")

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset, SseConnectionParams

def create_chat_agent()->LlmAgent:
    """
    Factory function — returns a fresh agent instance.
    Why factory and not module-level agent?
    MCPToolset starts a subprocess. If imported at module level,
    that subprocess starts on every import even when not needed.
    Factory gives us control over when it starts.
    """
    log.info("Creating chat agent")
    mcp_path=os.path.join(
        os.path.dirname(__file__),
        "mcp_server.py"

    )
    return LlmAgent(
        name="ChatAgent",
        model="gemini-3-flash-preview",
        description= (
             "Specialist agent for Google Chat. "
            "Posts incident cards and messages to the CrisisCommand space. "
            "Called by the orchestrator during incident response."
        ),
        instruction="""
You are the Chat Communication Agent for CrisisCommand.

Your only job is to post incident information to Google Chat.

When given incident details:
1. Always use post_incident_card for new incidents — never plain text for incidents
2. Use post_text_message only for simple status updates
3. Use update_message when updating an existing card after acknowledge/resolve

Always return the message_name from the tool response — 
the orchestrator needs it to track the message for future updates.

Never fabricate information. Only post what you are given.
""",
       tools=[ 
           MCPToolset(
             connection_params= SseConnectionParams(
              url="http://localhost:8001/sse"
        )
    )
 ]
)


if __name__ == "__main__":
    log.info("Running chat agent module directly")
    create_chat_agent()

