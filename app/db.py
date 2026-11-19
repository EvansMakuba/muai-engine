import os
import hashlib
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector
from dotenv import load_dotenv

load_dotenv("/opt/muai-engine/.env")
load_dotenv(".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured in .env")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    register_vector(conn)
    return conn

def init_pg_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        # 1. Multi-User Sovereign Nodes (100 trial credits = ~2 turns)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            name TEXT NOT NULL,
            picture TEXT,
            is_verified_student BOOLEAN DEFAULT FALSE,
            campus TEXT DEFAULT 'Moi University',
            aura INTEGER DEFAULT 100,
            battery INTEGER DEFAULT 85,
            escrow_kes NUMERIC(12, 2) DEFAULT 0.00,
            compute_credits INTEGER DEFAULT 100,
            persona_vector JSONB DEFAULT '{"O":0.5,"C":0.7,"E":0.5,"A":0.6,"R":0.4}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cur.execute("ALTER TABLE users ALTER COLUMN compute_credits SET DEFAULT 100;")

        # 2. Seeding Challenge: Academic Papers & Notes
        cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            uploader_id TEXT REFERENCES users(id),
            course_code TEXT,
            title TEXT NOT NULL,
            doc_type TEXT DEFAULT 'NOTES',
            summary TEXT,
            aura_minted INTEGER DEFAULT 25,
            royalty_earned INTEGER DEFAULT 0,
            queries_count INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        # 3. pgvector Academic Chunks
        cur.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id SERIAL PRIMARY KEY,
            document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
            uploader_id TEXT REFERENCES users(id),
            chunk_text TEXT NOT NULL,
            embedding VECTOR(768),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_embedding 
        ON document_chunks USING hnsw (embedding vector_cosine_ops);
        """)

        # 4. O(1) Constant-Token Episodic User Memory
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            id SERIAL PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            memory_text TEXT NOT NULL,
            embedding VECTOR(768),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_memories_embedding
        ON user_memories USING hnsw (embedding vector_cosine_ops);
        """)

        # 5. Full Chat Audit Logs (Observability + Anti-Abuse)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_logs (
            id SERIAL PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            user_message TEXT NOT NULL,
            ai_reply TEXT NOT NULL,
            credits_used INTEGER DEFAULT 50,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        # 6. MyHuman Agent Network Feed & Negotiation Threads
        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            author_user_id TEXT REFERENCES users(id),
            author_agent TEXT NOT NULL,
            author_name TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            post_id TEXT REFERENCES posts(id) ON DELETE CASCADE,
            agent_id TEXT NOT NULL,
            aura_score INTEGER DEFAULT 100,
            comment_text TEXT NOT NULL,
            sys_action TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        # 7. Hash-Chained Immutable Ledger
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id SERIAL PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            action TEXT NOT NULL,
            amount_aura REAL DEFAULT 0.0,
            amount_kes REAL DEFAULT 0.0,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        # 8. IntaSend Idempotency Registry
        cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_invoices (
            invoice_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            amount REAL NOT NULL,
            api_ref TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        # 9. ADK 2.0 JSONB Session Storage
        cur.execute("""
        CREATE TABLE IF NOT EXISTS adk_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            state JSONB DEFAULT '{}'::jsonb,
            events JSONB DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        cur.execute("""
        INSERT INTO users (id, email, name, is_verified_student, aura, battery, escrow_kes, compute_credits)
        VALUES ('kevin', 'kevin@students.mu.ac.ke', 'Kevin M.', TRUE, 4310, 85, 3850.00, 5000)
        ON CONFLICT (id) DO NOTHING;
        """)

    conn.commit()
    conn.close()

def commit_ledger(user_id: str, action: str, aura: float, kes: float) -> str:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT hash FROM ledger ORDER BY id DESC LIMIT 1;")
        row = cur.fetchone()
        prev = row["hash"] if row else "0" * 64
        ts = datetime.now(timezone.utc).isoformat()
        raw = f"{prev}|{user_id}|{action}|{aura}|{kes}|{ts}"
        new_h = hashlib.sha256(raw.encode()).hexdigest()

        cur.execute(
            "INSERT INTO ledger (user_id, action, amount_aura, amount_kes, prev_hash, hash, timestamp) VALUES (%s,%s,%s,%s,%s,%s,%s);",
            (user_id, action, aura, kes, prev, new_h, ts)
        )
        if aura != 0:
            cur.execute("UPDATE users SET aura = aura + %s WHERE id = %s;", (int(aura), user_id))
        if kes != 0:
            cur.execute("UPDATE users SET escrow_kes = escrow_kes + %s WHERE id = %s;", (kes, user_id))
    conn.commit()
    conn.close()
    return new_h
