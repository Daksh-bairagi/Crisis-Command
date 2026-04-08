import httpx
import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from logger import get_logger

log = get_logger("simulator")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:8000/webhook")
SIMULATOR_SECRET = os.getenv("SIMULATOR_SECRET", "crisis-dev-secret")

# Pre-built incident scenarios for demo
INCIDENTS = {
    "p0_payments": {
        "type": "MONITORING_ALERT",
        "alert": {
            "service": "payments-service",
            "error_rate": 0.94,
            "latency_p99_ms": 8400,
            "affected_users": 12000,
            "requests_per_minute": 3400,
            "description": "Spike in 500 errors on checkout endpoint",
            "region": "asia-south1",
            "diagnostics": {
                "last_logs": [
                    "ERROR 14:23:01 PaymentService - DB connection timeout after 30s",
                    "ERROR 14:23:02 PaymentService - Retry 1/3 failed",
                    "FATAL 14:23:02 PaymentService - Circuit breaker opened"
                ],
                "cpu_usage": "94%",
                "memory_usage": "87%",
                "last_deployment": "deploy-447",
                "deployment_age_minutes": 8
            }
        }
    },
    "p1_auth": {
        "type": "MONITORING_ALERT",
        "alert": {
            "service": "auth-service",
            "error_rate": 0.12,
            "latency_p99_ms": 8200,
            "affected_users": 3400,
            "requests_per_minute": 890,
            "description": "Login latency spike, P99 at 8s vs 2s threshold",
            "region": "asia-south1",
            "diagnostics": {
                "last_logs": [
                    "WARN 14:20:01 AuthService - Token validation slow: 6200ms",
                    "WARN 14:20:45 AuthService - Redis cache miss rate 78%",
                    "ERROR 14:21:03 AuthService - Session store timeout"
                ],
                "cpu_usage": "61%",
                "memory_usage": "73%",
                "last_deployment": "deploy-445",
                "deployment_age_minutes": 142
            }
        }
    },
    "p2_storage": {
        "type": "MONITORING_ALERT",
        "alert": {
            "service": "storage-service",
            "error_rate": 0.0,
            "latency_p99_ms": 210,
            "affected_users": 0,
            "requests_per_minute": 120,
            "description": "Disk usage approaching threshold at 87%",
            "region": "asia-south1",
            "diagnostics": {
                "last_logs": [
                    "WARN 14:10:01 StorageService - Disk usage 87% on /data",
                    "WARN 14:10:01 StorageService - 13GB remaining of 100GB"
                ],
                "cpu_usage": "23%",
                "memory_usage": "41%",
                "last_deployment": "deploy-441",
                "deployment_age_minutes": 1840
            }
        }
    }
}

async def fire(scenario: str = "p0_payments"):
    """Fire a simulated incident alert"""
    
    if scenario not in INCIDENTS:
        log.error("Unknown scenario '%s'. Available: %s", scenario, list(INCIDENTS.keys()))
        return

    incident = INCIDENTS[scenario]
    log.info("🚀 Firing incident scenario: %s", scenario)
    log.info("Service: %s | Severity: %s", 
             incident["alert"]["service"])
            
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                WEBHOOK_URL,
                json=incident,
                headers={
                    "Content-Type": "application/json",
                    "x-simulator-secret": SIMULATOR_SECRET
                },
                timeout=10.0
            )
            
            log.info("Response status: %s", response.status_code)
            log.info("Response body: %s", response.json())
            
        except httpx.ConnectError:
            log.error("Could not connect to webhook at %s", WEBHOOK_URL)
            log.error("Is uvicorn running?")
        except Exception as e:
            log.error("Simulator error: %s", str(e))


if __name__ == "__main__":
    # Allow passing scenario from command line
    # python simulator/fire_incident.py p0_payments
    scenario = sys.argv[1] if len(sys.argv) > 1 else "p0_payments"
    asyncio.run(fire(scenario))