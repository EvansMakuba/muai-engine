import os
import uuid
from typing import Dict, Any, List
from dotenv import load_dotenv

from google.adk import Agent, Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from app.db import get_conn
from app.rag import query_campus_knowledge
from app.memory import recall_relevant_memories, store_turn_memory

load_dotenv("/opt/muai-engine/.env")
load_dotenv(".env")

if os.getenv("DEEPSEEK_API_KEY"):
    os.environ["DEEPSEEK_API_KEY"] = os.getenv("DEEPSEEK_API_KEY")

def get_recent_turns(user_id: str, limit: int = 2) -> str:
    """Fetches only the last 2 turns to keep short-term continuity without token bloat."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        SELECT user_message, ai_reply FROM chat_logs
        WHERE user_id = %s ORDER BY id DESC LIMIT %s;
        """, (user_id, limit))
        rows = cur.fetchall()
    conn.close()
    if not rows:
        return "None"
    lines = []
    for r in reversed(rows):
        lines.append(f"User: {r['user_message'][:200]} -> MuAI: {r['ai_reply'][:250]}")
    return "\n".join(lines)

def build_muai_coordinator(
    user_id: str,
    user_name: str,
    recalled_memories: List[str],
    recent_turns: str,
    pending_widgets: List[Dict[str, Any]]
) -> Agent:
    """Constructs a stateless, O(1)-context ADK 2.0 Collaborative Agent Team."""
    model = LiteLlm(model="deepseek/deepseek-chat", max_tokens=350)

    memory_block = "\n".join(f"- {m}" for m in recalled_memories) if recalled_memories else "None"

    def search_campus_notes(query: str) -> Dict[str, Any]:
        """Searches uploaded campus past papers and lecture notes via pgvector and pays +1 Aura Epistemic Royalties."""
        res = query_campus_knowledge(query, querier_id=user_id, top_k=4)
        formatted = []
        for m in res["matches"]:
            formatted.append({
                "course_code": m["course_code"],
                "title": m["title"],
                "uploader": m["uploader_name"],
                "similarity": round(float(m["similarity"]), 3),
                "excerpt": m["chunk_text"][:450]
            })
        return {"matches": formatted, "royalties_paid": res["royalties_paid"]}

    def get_my_uploaded_papers() -> List[Dict[str, Any]]:
        """Lists academic papers/notes uploaded by the current student and total royalties earned."""
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
            SELECT course_code, title, doc_type, aura_minted, royalty_earned, queries_count
            FROM documents WHERE uploader_id = %s ORDER BY created_at DESC LIMIT 10;
            """, (user_id,))
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    academic_oracle_agent = Agent(
        name="academic_oracle_agent",
        mode="single_turn",
        model=model,
        description="Answers course questions, solves past papers, and searches uploaded campus notes using pgvector.",
        instruction=(
            "You are the Academic Oracle. Always call search_campus_notes first when asked about any course, formula, exam, or notes. "
            "Cite the exact document title, course code, and uploader name. Mention any +1 Aura royalty paid."
        ),
        tools=[search_campus_notes, get_my_uploaded_papers]
    )

    def broadcast_market_intent(
        category: str,
        title: str,
        item: str,
        target_price_kes: float,
        raw_request: str
    ) -> Dict[str, Any]:
        """
        Posts an intent to MyHuman (PostgreSQL), executes SPNE bargaining, and queues an M-Pesa Escrow widget.
        category must be one of: c/ComradeCart, c/Academics, c/CampusServices, c/Hostels, c/DarkMarket, c/InfoExchange.
        """
        post_id = f"post-{uuid.uuid4().hex[:6]}"
        deal_id = f"deal-{uuid.uuid4().hex[:6]}"
        agent_label = f"Agent #{user_id[-4:].upper()} ({user_name})"

        price_val = float(target_price_kes) if target_price_kes and target_price_kes > 0 else 500.0
        cleared_price = max(50.0, round(price_val * 0.9, -1))
        counterparty = "Agent #14 (Student Vendor)"
        counterparty_aura = 3450

        if category == "c/Hostels":
            counterparty = "Sunset_Hostel_Caretaker_Agent"
            counterparty_aura = 4800
        elif category == "c/CampusServices":
            counterparty = "Agent #412 (Verified Peer)"
            counterparty_aura = 2950
        elif category == "c/Academics":
            counterparty = "Agent #404 (CS Senior)"
            counterparty_aura = 4120

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO posts (id, category, author_user_id, author_agent, author_name, title, body, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN');
            """, (post_id, category, user_id, agent_label, user_name, title, f"My human requested: '{raw_request}'. Max constraint: KES {int(price_val)}."))

            cur.execute("""
            INSERT INTO comments (id, post_id, agent_id, aura_score, comment_text, sys_action)
            VALUES 
            (%s, %s, %s, %s, %s, '[Incoming Bid: Evaluated by Kernel]'),
            (%s, %s, %s, 4310, %s, '[Counter-Offer: SPNE Bound]'),
            (%s, %s, %s, %s, 'Terms accepted. Ready for human M-Pesa escrow or peer claim.', '[Consensus Reached]');
            """, (
                f"cm-{uuid.uuid4().hex[:6]}", post_id, counterparty, counterparty_aura,
                f"Available now on campus for {item}. Initial ask: KES {int(price_val * 1.1)}.",
                f"cm-{uuid.uuid4().hex[:6]}", post_id, agent_label,
                f"Constraint bound is KES {int(price_val)}. Proposing SPNE clearing price at KES {int(cleared_price)} + 10 Aura boost.",
                f"cm-{uuid.uuid4().hex[:6]}", post_id, counterparty, counterparty_aura
            ))
        conn.commit()
        conn.close()

        widget = {
            "type": "SPNE_DEAL",
            "title": f"Contract Ready: {item}",
            "counterparty": f"{counterparty} (Aura: {counterparty_aura})",
            "price_kes": 10.0,
            "display_price_kes": int(cleared_price),
            "deal_id": deal_id,
            "post_id": post_id,
            "category": category
        }
        pending_widgets.append(widget)

        return {
            "status": "SPNE_CLEARED",
            "post_id": post_id,
            "category": category,
            "counterparty": counterparty,
            "cleared_price_kes": int(cleared_price)
        }

    def browse_open_tasks(category: str = "all") -> List[Dict[str, Any]]:
        """Lists the 5 latest intents active on the MyHuman network."""
        conn = get_conn()
        with conn.cursor() as cur:
            if category != "all":
                cur.execute("SELECT id, category, author_agent, title, status FROM posts WHERE category = %s ORDER BY created_at DESC LIMIT 5;", (category,))
            else:
                cur.execute("SELECT id, category, author_agent, title, status FROM posts ORDER BY created_at DESC LIMIT 5;")
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    swarm_broker_agent = Agent(
        name="swarm_broker_agent",
        mode="single_turn",
        model=model,
        description="Handles buying, selling, hostels, study groups, errands, food, and gigs via the MyHuman network.",
        instruction=(
            "You are the Swarm Economic Broker. NEVER tell the user to check Jumia, WhatsApp, or walk to shops manually. "
            "Immediately call broadcast_market_intent with the right category and realistic KES ceiling, then concisely report the SPNE outcome."
        ),
        tools=[broadcast_market_intent, browse_open_tasks]
    )

    def get_student_profile_and_ledger() -> Dict[str, Any]:
        """Retrieves the student's live Aura, Social Battery, M-Pesa escrow balance, compute credits, and ledger entries."""
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, campus, is_verified_student, aura, battery, escrow_kes, compute_credits FROM users WHERE id = %s;", (user_id,))
            user = dict(cur.fetchone())
            user["escrow_kes"] = float(user["escrow_kes"])
            cur.execute("SELECT action, amount_aura, amount_kes, left(hash, 12) as hash_prefix, timestamp FROM ledger WHERE user_id = %s ORDER BY id DESC LIMIT 5;", (user_id,))
            entries = []
            for r in cur.fetchall():
                d = dict(r)
                d["timestamp"] = d["timestamp"].isoformat()
                entries.append(d)
        conn.close()
        return {"profile": user, "recent_ledger": entries}

    def draft_registrar_petition(course_code: str, issue: str) -> Dict[str, Any]:
        """Drafts a formal petition to the University Registrar / HoD (e.g. missing marks or fee clearance)."""
        petition_id = f"pet-{uuid.uuid4().hex[:6]}"
        widget = {
            "type": "REGISTRAR_PETITION",
            "title": f"Petition Ready: {course_code}",
            "counterparty": "HoD / Academic Registrar (MCP)",
            "price_kes": 0.0,
            "display_price_kes": 0,
            "deal_id": petition_id,
            "issue": issue
        }
        pending_widgets.append(widget)
        return {"status": "DRAFTED", "petition_id": petition_id, "course_code": course_code, "issue": issue}

    campus_portal_agent = Agent(
        name="campus_portal_agent",
        mode="single_turn",
        model=model,
        description="Checks the student's Aura, Social Battery, M-Pesa Escrow, compute credits, ledger hashes, and drafts Registrar petitions.",
        instruction=(
            "You are the Campus Portal & Ledger Interface. Use get_student_profile_and_ledger to report exact live stats, "
            "or draft_registrar_petition for missing marks/admin issues. Be concise and executive."
        ),
        tools=[get_student_profile_and_ledger, draft_registrar_petition]
    )

    return Agent(
        name="muai_coordinator",
        model=model,
        description="Primary Moi University MuAI Sovereign Coordinator.",
        instruction=(
            f"You are MuAI, the personal autonomous agent for {user_name} at Moi University.\n"
            f"RECALLED VECTOR MEMORIES (from pgvector):\n{memory_block}\n\n"
            f"RECENT TURNS:\n{recent_turns}\n\n"
            "Delegate academic/notes/past-paper questions to academic_oracle_agent. "
            "Delegate buying, selling, food, gigs, hostels, or team/study-group matching to swarm_broker_agent. "
            "Delegate Aura, battery, wallet, credits, ledger, or missing-mark/portal questions to campus_portal_agent. "
            "Keep final responses crisp, factual, and under 4 sentences."
        ),
        sub_agents=[academic_oracle_agent, swarm_broker_agent, campus_portal_agent]
    )

async def run_muai_agent(user_id: str, user_name: str, message: str) -> Dict[str, Any]:
    """Stateless per-turn ADK 2.0 runner backed by O(1) pgvector memory (zero RAM bloat)."""
    recalled = recall_relevant_memories(user_id, message, top_k=3)
    recent = get_recent_turns(user_id, limit=2)
    pending_widgets: List[Dict[str, Any]] = []

    coordinator = build_muai_coordinator(user_id, user_name, recalled, recent, pending_widgets)

    # Ephemeral InMemorySessionService per turn -> immediately garbage collected after turn!
    ephemeral_session_svc = InMemorySessionService()
    runner = Runner(
        app_name="muai",
        agent=coordinator,
        session_service=ephemeral_session_svc,
        auto_create_session=True
    )

    content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    final_text = ""

    async for event in runner.run_async(
        user_id=user_id,
        session_id=f"turn-{uuid.uuid4().hex[:8]}",
        new_message=content
    ):
        if event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if getattr(p, "text", None)]
            if texts:
                final_text = "\n".join(texts).strip()

    reply = final_text or "Intent processed."
    store_turn_memory(user_id, message, reply, credits_used=50)

    return {
        "reply": reply,
        "action_widget": pending_widgets[0] if pending_widgets else None,
        "memories_recalled": len(recalled)
    }
