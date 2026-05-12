import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from agents.chat_agent.mcp_server import mcp


if __name__ == "__main__":
    mcp.run(transport="sse", host="127.0.0.1", port=8001, path="/sse", show_banner=False)
