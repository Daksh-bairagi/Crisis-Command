from dataclasses import dataclass
# dataclass is a decorator that automatically generates special methods like __init__() and __repr__() for classes. It is used to create classes that are primarily used to store data.
from typing import Optional
# optional is for either string or none
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
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
model= genai.GenerativeModel("gemini-3-flash-preview")

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





async def classify(alert: dict) -> IncidentClassification:
    incident_id = f"INC-{str(uuid.uuid4())[:8].upper()}"
    """
    Uses Gemini to reason about raw metrics and determine:
    - Severity (P0/P1/P2)
    - Likely cause
    - Which agents to activate
    - Suggested first action
    """
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
    log.info(f"Classifying incident  with alert: {json.dumps(alert)}")
    response= model.generate_content(prompt)
    raw=  response.text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    result = json.loads(raw)
    log.info("Classification: %s | Cause: %s", result["severity"], result["likely_cause"])

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
    

