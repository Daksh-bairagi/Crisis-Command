import asyncio
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, ".env"))

from google import genai
from database.db import (
    EMBEDDING_DIMENSION,
    ensure_incident_memory_vector_dimension,
    store_incident_memory,
    test_connection,
)
from logger import get_logger

log = get_logger("seeder")

client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

PAST_INCIDENTS = [
    {
        "incident_id": "INC-HIST001",
        "content": (
            "P0 incident on payments-service. DB connection pool exhausted "
            "after deploy-431. 94% error rate, 11000 users affected. "
            "Resolution: increased connection pool size from 10 to 50, "
            "restarted payments-service pods. MTTR: 23 minutes. "
            "Runbook: restart connection pool manager, check pool config."
        )
    },
    {
        "incident_id": "INC-HIST002",
        "content": (
            "P1 incident on auth-service. Redis cache unavailable causing "
            "session validation failures. 3200 users unable to login. "
            "Resolution: flushed Redis cache, restarted auth pods. "
            "MTTR: 14 minutes. Runbook: check Redis connectivity first."
        )
    },
    {
        "incident_id": "INC-HIST003",
        "content": (
            "P0 incident on payments-service. Circuit breaker opened after "
            "upstream database latency spike. Deploy-445 introduced N+1 query. "
            "12000 users affected, checkout completely down. "
            "Resolution: rolled back deploy-445, query optimized next sprint. "
            "MTTR: 31 minutes."
        )
    },
    {
        "incident_id": "INC-HIST004",
        "content": (
            "P1 incident on storage-service. Disk usage hit 95% on /data volume. "
            "Log rotation misconfigured after deploy-440. "
            "Resolution: cleared temp files, fixed log rotation config. "
            "MTTR: 8 minutes. Runbook: run cleanup_logs.sh script first."
        )
    },
    {
        "incident_id": "INC-HIST005",
        "content": (
            "P0 incident on api-gateway. Memory leak introduced in deploy-438 "
            "caused OOM kills across all gateway pods. "
            "8500 users affected, all API traffic down. "
            "Resolution: rolled back deploy-438, memory profiling added to CI. "
            "MTTR: 19 minutes."
        )
    },
    {
        "incident_id": "INC-HIST006",
        "content": (
            "P1 incident on auth-service. JWT signing key rotation caused "
            "all active sessions to invalidate. 5000 users logged out. "
            "Resolution: extended key rotation grace period to 24 hours. "
            "MTTR: 11 minutes. Runbook: always use rolling key rotation."
        )
    },
]


def get_embedding(text: str) -> list[float]:
    """Generate an embedding using Gemini's active embedding model."""
    response = client.models.embed_content(
        model="gemini-embedding-001",
        contents=text
    )
    return response.embeddings[0].values


async def seed():
    log.info("Testing DB connection...")
    ok = await test_connection()
    if not ok:
        log.error("DB not reachable. Is Docker running?")
        return

    log.info("Ensuring incident_memory.embedding uses vector(%d)...", EMBEDDING_DIMENSION)
    ok = await ensure_incident_memory_vector_dimension()
    if not ok:
        log.error("Vector column migration/setup failed.")
        return

    log.info("Seeding %d past incidents...", len(PAST_INCIDENTS))

    for incident in PAST_INCIDENTS:
        log.info("Embedding %s...", incident["incident_id"])
        embedding = get_embedding(incident["content"])
        if len(embedding) != EMBEDDING_DIMENSION:
            log.error(
                "Embedding model returned %d dimensions, expected %d.",
                len(embedding),
                EMBEDDING_DIMENSION,
            )
            return
        success = await store_incident_memory(
            incident_id=incident["incident_id"],
            content=incident["content"],
            embedding=embedding,
            source="past_incident"
        )
        if success:
            log.info("✅ Seeded %s", incident["incident_id"])
        else:
            log.error("❌ Failed %s", incident["incident_id"])

    log.info("Seeding complete.")


if __name__ == "__main__":
    asyncio.run(seed())
