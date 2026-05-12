from dataclasses import dataclass
from typing import Optional
import asyncio
import sys
import os
import json
from textwrap import dedent
import google.generativeai as genai
from dotenv import load_dotenv
import uuid

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from logger import get_logger

log = get_logger("classifier")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CLASSIFIER_TIMEOUT_SECONDS = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", "8"))
LLM_CLASSIFIER_ENABLED = (
    os.getenv("ENABLE_LLM_CLASSIFIER", "true" if os.getenv("K_SERVICE") else "false")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
else:
    model = None
    log.warning("GOOGLE_API_KEY is not configured. Using heuristic classifier fallback.")

@dataclass
class IncidentClassification:
    """
    Dataclass over dict - because structure matters.
    Every part of the system that receives this knows
    exactly what fields exist. No KeyError surprises.
    """

    incident_id: str
    severity: str
    service: str
    description: str
    likely_cause: str
    suggested_action: str
    affected_users: str
    region: str
    error_rate: str
    deployment_id: str
    activate_chat: bool
    activate_docs: bool
    activate_calendar: bool
    reasoning: str





def _is_structured_alert(alert: dict) -> bool:
    try:
        float(alert["error_rate"])
        int(alert["affected_users"])
        return True
    except (KeyError, TypeError, ValueError):
        return False


async def classify(alert: dict) -> IncidentClassification:
    """
    Uses Gemini to reason about raw metrics and determine:
    - Severity (P0/P1/P2)
    - Likely cause
    - Which agents to activate
    - Suggested first action
    """
    incident_id = f"INC-{str(uuid.uuid4())[:8].upper()}"

    if _is_structured_alert(alert):
        return _heuristic_classification(alert, incident_id, "structured alert — deterministic path")

    log.info("Unstructured alert — routing to LLM classifier")

    if not model:
        return _heuristic_classification(alert, incident_id, "GOOGLE_API_KEY is missing")

    if not LLM_CLASSIFIER_ENABLED:
        return _heuristic_classification(alert, incident_id, "local mode disables live LLM calls")

    prompt = f"""
        You are an SRE incident classifier. Analyze this alert and respond in JSON only.

        Alert data:
        {json.dumps(alert, indent=2)}

        Respond with exactly this JSON structure:
        {{
            "severity": "P0|P1|P2",
            "likely_cause": "one sentence",
            "activate_chat": true,
            "activate_docs": true|false,
            "activate_calendar": true|false,
            "suggested_action": "one sentence",
            "reasoning": "2-3 sentences explaining your classification"
        }}

        Rules:

        Severity should be determined using multiple signals, not a single threshold.

        P0 (Critical):
        - Service is completely down OR returning mostly failures (>70%)
        - Affects critical services (payments, authentication, core APIs)
        - Large user impact (>10,000 users OR global outage)
        - Sudden spike in errors or latency
        - Revenue or login functionality is impacted

        P1 (High):
        - Partial outage or degraded performance
        - Error rate between 20%-70%
        - Moderate user impact (1,000-10,000 users)
        - High latency affecting user experience
        - Non-critical but important service degradation

        P2 (Medium/Low):
        - Minor issues, low error rates (<20%)
        - Limited user impact (<1,000 users)
        - No major business impact
        - Background or internal service issues

        Additional reasoning requirements:
        - Consider BOTH error_rate AND affected_users together
        - Consider service criticality (payments/auth > others)
        - Consider sudden spikes vs stable issues
        - Prefer higher severity if uncertain (fail-safe bias)
        - If data is incomplete, make reasonable assumptions and explain them

        Agent activation:
        - Always activate chat
        - Activate docs for P0 and P1
        - Activate calendar only for P0
        """

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(model.generate_content, prompt),
            timeout=CLASSIFIER_TIMEOUT_SECONDS,
        )
        raw = _normalize_response_text(response.text)
        result = json.loads(raw)
        log.info("Classification: %s | Cause: %s", result["severity"], result["likely_cause"])
        return _build_classification(alert, incident_id, result)
    except Exception as exc:
        log.warning("LLM classifier unavailable, using heuristic fallback: %s", exc)
        return _heuristic_classification(alert, incident_id, str(exc))


def _normalize_response_text(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return cleaned.strip()


def _build_classification(alert: dict, incident_id: str, result: dict) -> IncidentClassification:
    return IncidentClassification(
        incident_id=incident_id,
        severity=result["severity"],
        service=alert.get("service", "unknown"),
        description=alert.get("description", ""),
        likely_cause=result["likely_cause"],
        suggested_action=result["suggested_action"],
        affected_users=str(alert.get("affected_users", "0")),
        region=alert.get("region", "unknown"),
        error_rate=str(alert.get("error_rate", "0")),
        deployment_id=alert.get("diagnostics", {}).get("last_deployment", "unknown"),
        activate_chat=result["activate_chat"],
        activate_docs=result["activate_docs"],
        activate_calendar=result["activate_calendar"],
        reasoning=result["reasoning"]
    )


def _heuristic_classification(alert: dict, incident_id: str, fallback_reason: str) -> IncidentClassification:
    service = str(alert.get("service", "unknown"))
    description = str(alert.get("description", ""))
    service_lower = service.lower()

    try:
        error_rate = float(alert.get("error_rate", 0) or 0)
    except (TypeError, ValueError):
        error_rate = 0.0

    try:
        affected_users = int(float(alert.get("affected_users", 0) or 0))
    except (TypeError, ValueError):
        affected_users = 0

    critical_markers = ("payment", "payments", "checkout", "auth", "login", "gateway")
    is_critical_service = any(marker in service_lower for marker in critical_markers)

    if error_rate >= 0.7 or affected_users >= 10000 or (is_critical_service and error_rate >= 0.4):
        severity = "P0"
    elif error_rate >= 0.2 or affected_users >= 1000 or (is_critical_service and error_rate >= 0.1):
        severity = "P1"
    else:
        severity = "P2"

    likely_cause = (
        "Likely regression after a recent deployment or a dependency/configuration issue."
        if alert.get("diagnostics", {}).get("last_deployment")
        else "Likely service degradation caused by an application error, dependency issue, or unhealthy downstream dependency."
    )
    suggested_action = {
        "P0": "Freeze further deploys, check the latest changes, and inspect application plus database connectivity immediately.",
        "P1": "Inspect recent changes, application logs, and dependency health, then roll back or patch the affected service if needed.",
        "P2": "Review logs and metrics for the affected path and confirm whether this is isolated or the start of a wider regression.",
    }[severity]
    reasoning = (
        f"Used local heuristic fallback because the LLM classifier was unavailable ({fallback_reason}). "
        f"Severity was inferred from error_rate={error_rate}, affected_users={affected_users}, "
        f"and service_criticality={is_critical_service}."
    )

    return IncidentClassification(
        incident_id=incident_id,
        severity=severity,
        service=service,
        description=description,
        likely_cause=likely_cause,
        suggested_action=suggested_action,
        affected_users=str(affected_users),
        region=str(alert.get("region", "unknown")),
        error_rate=str(alert.get("error_rate", "0")),
        deployment_id=alert.get("diagnostics", {}).get("last_deployment", "unknown"),
        activate_chat=True,
        activate_docs=severity in {"P0", "P1"},
        activate_calendar=severity == "P0",
        reasoning=reasoning,
    )
