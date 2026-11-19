from typing import List
from app.db import get_conn
from app.rag import embed_query, embed_document_chunk, vec_to_pg

def recall_relevant_memories(user_id: str, query_text: str, top_k: int = 3) -> List[str]:
    """Retrieves ONLY the top-k semantically relevant past turns for this user (O(1) prompt size)."""
    try:
        q_vec = embed_query(query_text)
        vec_str = vec_to_pg(q_vec)
    except Exception:
        return []

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        SELECT memory_text, 1 - (embedding <=> %s::vector) AS similarity
        FROM user_memories
        WHERE user_id = %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s;
        """, (vec_str, user_id, vec_str, top_k))
        rows = cur.fetchall()
    conn.close()

    return [r["memory_text"] for r in rows if float(r["similarity"]) >= 0.35]

def store_turn_memory(user_id: str, user_msg: str, ai_reply: str, credits_used: int = 50) -> None:
    """Persists full chat turn in chat_logs and embeds compact episodic memory into pgvector."""
    compact_text = f"User: {user_msg[:300]} | MuAI: {ai_reply[:400]}"

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO chat_logs (user_id, user_message, ai_reply, credits_used)
        VALUES (%s, %s, %s, %s);
        """, (user_id, user_msg, ai_reply, credits_used))
    conn.commit()
    conn.close()

    try:
        m_vec = embed_document_chunk(compact_text, title=f"memory-{user_id}")
        vec_str = vec_to_pg(m_vec)
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO user_memories (user_id, memory_text, embedding)
            VALUES (%s, %s, %s::vector);
            """, (user_id, compact_text, vec_str))
        conn.commit()
        conn.close()
    except Exception:
        pass
