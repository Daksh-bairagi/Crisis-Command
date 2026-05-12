import os
import sys
from pathlib import Path

from fastmcp.client import Client, PythonStdioTransport

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / ".mcp-logs"
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()

SERVER_URLS = {
    "chat-mcp": os.getenv("CHAT_MCP_URL", "http://127.0.0.1:8001/sse"),
    "docs-mcp": os.getenv("DOCS_MCP_URL", "http://127.0.0.1:8002/sse"),
    "calendar-mcp": os.getenv("CALENDAR_MCP_URL", "http://127.0.0.1:8003/sse"),
    "github-mcp": os.getenv("GITHUB_MCP_URL", "http://127.0.0.1:8004/sse"),
    "logging-mcp": os.getenv("LOGGING_MCP_URL", "http://127.0.0.1:8005/sse"),
}


def _client_env() -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    root_str = str(ROOT_DIR)
    env["PYTHONPATH"] = (
        f"{root_str}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else root_str
    )
    return env


def create_mcp_client(script_relative_path: str, name: str) -> Client:
    if MCP_TRANSPORT == "sse":
        return Client(SERVER_URLS[name], name=name)

    if MCP_TRANSPORT != "stdio":
        raise ValueError(f"Unsupported MCP_TRANSPORT: {MCP_TRANSPORT}")

    LOG_DIR.mkdir(exist_ok=True)
    transport = PythonStdioTransport(
        script_path=ROOT_DIR / script_relative_path,
        python_cmd=sys.executable,
        cwd=str(ROOT_DIR),
        env=_client_env(),
        log_file=LOG_DIR / f"{name}.stderr.log",
        keep_alive=True,
    )
    return Client(transport, name=name)
