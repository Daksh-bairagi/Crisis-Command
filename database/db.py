import asyncio 
import os 
import sys
from datetime import datetime
from typing import Optional
import json

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from dotenv import load_dotenv

ROOT_DIR= os.path.dirname(os.path.dirname((os.path.abspath(__file__))))
sys.path.insert(0, ROOT_DIR)
load_dotenv(dotenv_path=os.path.join(ROOT_DIR, ".env"))

from logger import get_logger
log =   get_logger("database")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:crisis_dev@localhost:5432/crisiscommand")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

EMBEDDING_DIMENSION = 3072
INCIDENT_MEMORY_INDEX_NAME = "incident_memory_embedding_idx"
INCIDENT_MEMORY_VECTOR_TYPE = f"vector({EMBEDDING_DIMENSION})"
INCIDENT_MEMORY_HALFVEC_TYPE = f"halfvec({EMBEDDING_DIMENSION})"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)

# ─── INCIDENT OPERATIONS ────────────────────────────────────────────────────

async def store_incident(classification) -> bool:
    """Store a new incident from classifier output"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO incidents 
                (id, service, severity, description, likely_cause, 
                 suggested_action, affected_users, region, error_rate, 
                 deployment_id, status)
                VALUES 
                (:id, :service, :severity, :description, :likely_cause,
                 :suggested_action, :affected_users, :region, :error_rate,
                 :deployment_id, 'active')
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": classification.incident_id,
                "service": classification.service,
                "severity": classification.severity,
                "description": classification.description,
                "likely_cause": classification.likely_cause,
                "suggested_action": classification.suggested_action,
                "affected_users": classification.affected_users,
                "region": classification.region,
                "error_rate": classification.error_rate,
                "deployment_id": classification.deployment_id,
            })
            await session.commit()
            log.info("Stored incident %s", classification.incident_id)
            return True
    except Exception as e:
        log.error("Failed to store incident: %s", str(e))
        return False


async def get_active_incident_for_service(service: str) -> Optional[dict]:
    """
    Alert correlation — check if active incident already exists for this service.
    If yes, we append to it instead of creating a new one.
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT id, severity, status, detected_at
                FROM incidents
                WHERE service = :service 
                AND status = 'active'
                ORDER BY detected_at DESC
                LIMIT 1
            """), {"service": service})
            row = result.fetchone()
            if row:
                return {
                    "incident_id": row[0],
                    "severity": row[1],
                    "status": row[2],
                    "detected_at": str(row[3])
                }
            return None
    except Exception as e:
        log.error("Failed to check active incidents: %s", str(e))
        return None


async def update_incident_status(
    incident_id: str,
    status: str,
    chat_message_name: str = None,
    doc_url: str = None,
    meet_url: str = None
) -> bool:
    """Update incident status and optional metadata"""
    try:
        async with AsyncSessionLocal() as session:
            updates = {"status": status, "id": incident_id}
            set_clauses = ["status = :status"]

            if status == "acknowledged":
                set_clauses.append("acknowledged_at = NOW()")
            elif status == "resolved":
                set_clauses.append("resolved_at = NOW()")
                set_clauses.append("""
                    mttr_seconds = EXTRACT(EPOCH FROM (NOW() - detected_at))::INTEGER
                """)

            if chat_message_name:
                set_clauses.append("chat_message_name = :chat_message_name")
                updates["chat_message_name"] = chat_message_name

            if doc_url:
                set_clauses.append("doc_url = :doc_url")
                updates["doc_url"] = doc_url

            if meet_url:
                set_clauses.append("meet_url = :meet_url")
                updates["meet_url"] = meet_url

            query = f"UPDATE incidents SET {', '.join(set_clauses)} WHERE id = :id"
            await session.execute(text(query), updates)
            await session.commit()
            log.info("Updated incident %s → %s", incident_id, status)
            return True
    except Exception as e:
        log.error("Failed to update incident: %s", str(e))
        return False


async def get_incident(incident_id: str) -> Optional[dict]:
    """Fetch full incident record"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT id, service, severity, description, likely_cause,
                       suggested_action, affected_users, status,
                       detected_at, acknowledged_at, resolved_at, mttr_seconds,
                       chat_message_name, doc_url, meet_url
                FROM incidents WHERE id = :id
            """), {"id": incident_id})
            row = result.fetchone()
            if not row:
                return None
            return {
                "incident_id": row[0], "service": row[1],
                "severity": row[2], "description": row[3],
                "likely_cause": row[4], "suggested_action": row[5],
                "affected_users": row[6], "status": row[7],
                "detected_at": str(row[8]), "acknowledged_at": str(row[9]),
                "resolved_at": str(row[10]), "mttr_seconds": row[11],
                "chat_message_name": row[12], "doc_url": row[13],
                "meet_url": row[14]
            }
    except Exception as e:
        log.error("Failed to get incident: %s", str(e))
        return None


# ─── VECTOR MEMORY OPERATIONS ────────────────────────────────────────────────

async def ensure_incident_memory_vector_dimension() -> bool:
    """Resize incident_memory.embedding to match the active embedding model."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON a.attrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE c.relname = 'incident_memory'
                  AND n.nspname = current_schema()
                  AND a.attname = 'embedding'
                  AND a.attnum > 0
                  AND NOT a.attisdropped
            """))
            current_vector_type = result.scalar_one_or_none()

            if current_vector_type is None:
                log.error(
                    "incident_memory.embedding column not found. Apply database/schema.sql first."
                )
                return False

            if current_vector_type == INCIDENT_MEMORY_VECTOR_TYPE:
                return True

            log.warning(
                "Migrating incident_memory.embedding from %s to %s and clearing old embeddings.",
                current_vector_type,
                INCIDENT_MEMORY_VECTOR_TYPE,
            )
            await session.execute(text(f"DROP INDEX IF EXISTS {INCIDENT_MEMORY_INDEX_NAME}"))
            await session.execute(text("TRUNCATE TABLE incident_memory RESTART IDENTITY"))
            await session.execute(text(
                f"ALTER TABLE incident_memory ALTER COLUMN embedding TYPE {INCIDENT_MEMORY_VECTOR_TYPE}"
            ))
            await session.execute(text(f"""
                CREATE INDEX IF NOT EXISTS {INCIDENT_MEMORY_INDEX_NAME}
                ON incident_memory USING hnsw ((embedding::{INCIDENT_MEMORY_HALFVEC_TYPE}) halfvec_cosine_ops)
            """))
            await session.commit()
            log.info(
                "incident_memory.embedding is now using %s with a half-precision search index.",
                INCIDENT_MEMORY_VECTOR_TYPE,
            )
            return True
    except Exception as e:
        log.error("Failed to migrate incident_memory embedding dimension: %s", str(e))
        return False


async def store_incident_memory(
    incident_id: str,
    content: str,
    embedding: list[float],
    source: str = "past_incident"
) -> bool:
    """Store incident summary with embedding for RAG"""
    if len(embedding) != EMBEDDING_DIMENSION:
        log.error(
            "Embedding for %s has %d dimensions, expected %d.",
            incident_id,
            len(embedding),
            EMBEDDING_DIMENSION,
        )
        return False

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO incident_memory (incident_id, content, embedding, source)
                VALUES (:incident_id, :content, :embedding, :source)
            """), {
                "incident_id": incident_id,
                "content": content,
                "embedding": str(embedding),
                "source": source
            })
            await session.commit()
            log.info("Stored memory for incident %s", incident_id)
            return True
    except Exception as e:
        log.error("Failed to store memory: %s", str(e))
        return False


async def search_similar_incidents(
    query_embedding: list[float],
    limit: int = 3
) -> list[dict]:
    """
    Vector similarity search — the RAG query.
    Returns most similar past incidents to current alert.
    """
    if len(query_embedding) != EMBEDDING_DIMENSION:
        log.error(
            "Query embedding has %d dimensions, expected %d.",
            len(query_embedding),
            EMBEDDING_DIMENSION,
        )
        return []

    try:
        async with AsyncSessionLocal() as session:
            candidate_limit = max(limit * 5, 20)
            result = await session.execute(text(f"""
                WITH nearest_results AS MATERIALIZED (
                    SELECT content, source, incident_id,
                           embedding <=> CAST(:query_embedding AS {INCIDENT_MEMORY_VECTOR_TYPE}) AS distance
                    FROM incident_memory
                    ORDER BY embedding::{INCIDENT_MEMORY_HALFVEC_TYPE}
                             <=> CAST(:query_embedding AS {INCIDENT_MEMORY_HALFVEC_TYPE})
                    LIMIT :candidate_limit
                )
                SELECT content, source, incident_id, distance
                FROM nearest_results
                ORDER BY distance
                LIMIT :limit
            """), {
                "query_embedding": str(query_embedding),
                "candidate_limit": candidate_limit,
                "limit": limit
            })
            rows = result.fetchall()
            return [
                {
                    "content": row[0],
                    "source": row[1],
                    "incident_id": row[2],
                    "distance": float(row[3])
                }
                for row in rows
            ]
    except Exception as e:
        log.error("Vector search failed: %s", str(e))
        return []


# ─── TRACE OPERATIONS ────────────────────────────────────────────────────────

async def log_trace(
    session_id: str,
    agent_name: str,
    action: str,
    input_data: dict = None,
    output_data: dict = None,
    duration_ms: int = 0
) -> bool:
    """Log agent action to trace table — powers the trace UI"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO agent_traces
                (session_id, agent_name, action, input_data, output_data, duration_ms)
                VALUES (:session_id, :agent_name, :action, :input_data, :output_data, :duration_ms)
            """), {
                "session_id": session_id,
                "agent_name": agent_name,
                "action": action,
                "input_data": json.dumps(input_data or {}),
                "output_data": json.dumps(output_data or {}),
                "duration_ms": duration_ms
            })
            await session.commit()
            return True
    except Exception as e:
        log.error("Failed to log trace: %s", str(e))
        return False


async def get_recent_traces(limit: int = 50) -> list[dict]:
    """Fetch recent traces for the trace UI"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                SELECT session_id, agent_name, action, 
                       input_data, output_data, duration_ms, timestamp
                FROM agent_traces
                ORDER BY timestamp DESC
                LIMIT :limit
            """), {"limit": limit})
            rows = result.fetchall()
            return [
                {
                    "session_id": row[0],
                    "agent_name": row[1],
                    "action": row[2],
                    "input_data": row[3],
                    "output_data": row[4],
                    "duration_ms": row[5],
                    "timestamp": str(row[6])
                }
                for row in rows
            ]
    except Exception as e:
        log.error("Failed to get traces: %s", str(e))
        return []


# ─── TEST ────────────────────────────────────────────────────────────────────

async def test_connection():
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT version()"))
            version = result.fetchone()[0]
            log.info("DB connected: %s", version[:50])
            return True
    except Exception as e:
        log.error("DB connection failed: %s", str(e))
        return False


if __name__ == "__main__":
    asyncio.run(test_connection())
