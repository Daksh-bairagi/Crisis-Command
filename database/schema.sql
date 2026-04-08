-- Enable vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Core incident tracking
CREATE TABLE IF NOT EXISTS incidents (
    id VARCHAR(20) PRIMARY KEY,           -- INC-XXXXXXXX
    service VARCHAR(100) NOT NULL,
    severity VARCHAR(5) NOT NULL,         -- P0, P1, P2
    description TEXT,
    likely_cause TEXT,
    suggested_action TEXT,
    affected_users VARCHAR(50),
    region VARCHAR(50),
    error_rate VARCHAR(20),
    deployment_id VARCHAR(100),
    status VARCHAR(20) DEFAULT 'active',  -- active/acknowledged/resolved
    chat_message_name VARCHAR(255),       -- for updating Chat message
    doc_url VARCHAR(500),
    meet_url VARCHAR(500),
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    mttr_seconds INTEGER                  -- calculated on resolve
);

-- Vector memory for RAG
CREATE TABLE IF NOT EXISTS incident_memory (
    id SERIAL PRIMARY KEY,
    incident_id VARCHAR(20),
    content TEXT NOT NULL,                -- summary of the incident
    embedding vector(3072),              -- gemini-embedding-001 dimension
    source VARCHAR(50),                   -- 'past_incident', 'runbook'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS incident_memory_embedding_idx 
ON incident_memory USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);

-- Agent execution trace
CREATE TABLE IF NOT EXISTS agent_traces (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(50),
    agent_name VARCHAR(50),
    action TEXT,
    input_data JSONB,
    output_data JSONB,
    duration_ms INTEGER,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
