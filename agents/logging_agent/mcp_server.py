from fastmcp import FastMCP
import sys
import os
import re
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)

from logger import get_mcp_logger

log = get_mcp_logger("logging_mcp_server")
load_dotenv(os.path.join(ROOT_DIR, ".env"))

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")

mcp = FastMCP("logging-server")


def _project_available() -> bool:
    """Return True if GOOGLE_CLOUD_PROJECT is configured."""
    return bool(GOOGLE_CLOUD_PROJECT)


def _get_logging_service():
    """Build and return a Google Cloud Logging API client."""
    from googleapiclient.discovery import build
    from auth.oauth import get_credentials

    creds = get_credentials()
    return build("logging", "v2", credentials=creds)


def _demo_entries(service_name: str, minutes_back: int) -> list[dict]:
    """
    Return realistic demo log entries for the p0_payments DB pool exhaustion scenario.
    Shows ~15 entries across 3 pods ordered newest-first, covering the last 10 minutes.
    """
    now = datetime.now(timezone.utc)
    container = service_name if service_name else "payments-service"

    pod1 = f"{service_name}-pod-1"
    pod2 = f"{service_name}-pod-2"
    pod3 = f"{service_name}-pod-3"

    # Each tuple: (minutes_ago, severity, message, pod)
    _raw = [
        (1,  "ERROR",    "DB connection timeout after 30s — pool exhausted (active=10/10)",     pod1),
        (1,  "ERROR",    "DB connection timeout after 30s — pool exhausted (active=10/10)",     pod2),
        (2,  "CRITICAL", "Circuit breaker OPENED — failure rate 94% exceeds threshold 50%",     pod1),
        (2,  "ERROR",    "DB connection timeout after 30s — pool exhausted (active=10/10)",     pod3),
        (3,  "CRITICAL", "Circuit breaker OPENED — failure rate 94% exceeds threshold 50%",     pod2),
        (3,  "ERROR",    "Failed to acquire DB connection from pool within 30s",                 pod3),
        (4,  "CRITICAL", "Circuit breaker OPENED — failure rate 94% exceeds threshold 50%",     pod3),
        (5,  "ERROR",    "DB pool saturation: all 10 connections active, 47 requests queued",   pod1),
        (5,  "ERROR",    "DB pool saturation: all 10 connections active, 38 requests queued",   pod2),
        (6,  "WARNING",  "DB pool high-water mark reached (active=9/10) — latency degrading",   pod1),
        (6,  "WARNING",  "DB pool high-water mark reached (active=9/10) — latency degrading",   pod3),
        (7,  "ERROR",    "Slow query detected: SELECT * FROM payment_transactions took 12.4s",  pod2),
        (8,  "WARNING",  "DB pool connections: active=8/10 — approaching limit post deploy-447", pod1),
        (9,  "INFO",     "Deployment deploy-447 applied — DB_POOL_SIZE changed 50→10",          pod1),
        (10, "INFO",     "Service startup complete — DB pool initialized with max_size=10",      pod1),
    ]

    entries = []
    for minutes_ago, severity, message, pod in _raw:
        if minutes_ago <= minutes_back:
            entries.append(
                {
                    "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(),
                    "severity": severity,
                    "message": message,
                    "pod": pod,
                    "container": container,
                }
            )

    return entries


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@mcp.tool()
def search_logs(service_name: str, query: str, minutes_back: int = 15) -> dict:
    """
    Search application logs in Google Cloud Logging for a service.
    Use this to get full log context beyond the 3 lines in the alert.
    Call this when you need to understand error frequency, which pods are affected,
    or whether an issue started before or after a deployment.

    Args:
        service_name: The service to search logs for (e.g. 'payments-service')
        query: Log filter query — use error keywords from the alert
               (e.g. 'timeout OR pool OR "circuit breaker"')
        minutes_back: How many minutes of logs to retrieve (default 15)

    Returns dict with 'entries' list, each entry has timestamp, severity, message, pod, container.
    """
    minutes_back = max(1, minutes_back)

    if not re.match(r'^[a-zA-Z0-9._-]+$', service_name or ""):
        return {
            "success": False,
            "service_name": service_name,
            "query": query,
            "minutes_back": minutes_back,
            "total_count": 0,
            "time_range": {"start": "", "end": ""},
            "entries": [],
            "demo_mode": False,
            "error": f"Invalid service_name '{service_name}': only alphanumerics, dots, hyphens, underscores allowed",
        }

    now = datetime.now(timezone.utc)
    start_time = now - timedelta(minutes=minutes_back)

    if not _project_available():
        log.info(
            "Demo mode: GOOGLE_CLOUD_PROJECT not set — returning demo log entries for '%s'",
            service_name,
        )
        entries = _demo_entries(service_name, minutes_back)
        return {
            "success": True,
            "service_name": service_name,
            "query": query,
            "minutes_back": minutes_back,
            "total_count": len(entries),
            "time_range": {
                "start": start_time.isoformat(),
                "end": now.isoformat(),
            },
            "entries": entries,
            "demo_mode": True,
        }

    try:
        log.info(
            "Searching Cloud Logging: project=%s service=%s query='%s' minutes_back=%d",
            GOOGLE_CLOUD_PROJECT,
            service_name,
            query,
            minutes_back,
        )

        service = _get_logging_service()

        start_rfc3339 = start_time.isoformat().replace("+00:00", "Z")
        filter_str = (
            f'resource.labels.container_name="{service_name}" '
            f'AND timestamp>="{start_rfc3339}" '
            f'AND ({query})'
        )

        response = (
            service.entries()
            .list(
                body={
                    "resourceNames": [f"projects/{GOOGLE_CLOUD_PROJECT}"],
                    "filter": filter_str,
                    "orderBy": "timestamp desc",
                    "pageSize": 50,
                }
            )
            .execute()
        )

        raw_entries = response.get("entries", [])

        entries = [
            {
                "timestamp": entry.get("timestamp"),
                "severity": entry.get("severity", "DEFAULT"),
                "message": entry.get("textPayload") or str(entry.get("jsonPayload", "")),
                "pod": entry.get("resource", {}).get("labels", {}).get("pod_name", "unknown"),
                "container": entry.get("resource", {}).get("labels", {}).get(
                    "container_name", service_name
                ),
            }
            for entry in raw_entries[:20]  # cap at 20 entries
        ]

        log.info(
            "Cloud Logging returned %d entries for service '%s'", len(entries), service_name
        )

        return {
            "success": True,
            "service_name": service_name,
            "query": query,
            "minutes_back": minutes_back,
            "total_count": len(entries),
            "time_range": {
                "start": start_time.isoformat(),
                "end": now.isoformat(),
            },
            "entries": entries,
            "demo_mode": False,
        }

    except Exception as e:
        log.error("Error searching Cloud Logging for service '%s': %s", service_name, e)
        return {
            "success": False,
            "service_name": service_name,
            "query": query,
            "minutes_back": minutes_back,
            "total_count": 0,
            "time_range": {"start": start_time.isoformat(), "end": now.isoformat()},
            "entries": [],
            "demo_mode": False,
            "error": str(e),
        }


log.info(
    "Cloud Logging MCP ready — %s mode | project: %s",
    "live" if _project_available() else "DEMO",
    GOOGLE_CLOUD_PROJECT or "not set",
)

if __name__ == "__main__":
    log.info("Cloud Logging MCP server starting on stdio transport")
    mcp.run()
