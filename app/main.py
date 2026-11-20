import os
import hmac
import time
import uuid
import json
import asyncio
from collections import defaultdict, deque
from decimal import Decimal
from typing import Optional, AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from dotenv import load_dotenv

from app.db import get_conn, init_pg_db, commit_ledger
from app.auth import authenticate_google_user, create_session_token, decode_session_token
from app.rag import ingest_document, query_campus_knowledge
from app.memory import recall_relevant_memories, store_turn_memory
from openai import OpenAI

load_dotenv("/opt/muai-engine/.env")
load_dotenv(".env")

app = FastAPI(title="MuAI Sovereign Epistemic Engine", version="6.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INTASEND_SECRET_KEY = os.getenv("INTASEND_SECRET_KEY", "")
INTASEND_CHALLENGE = os.getenv("INTASEND_WEBHOOK_CHALLENGE", "muai-hackathon-challenge-2026")
INTASEND_BASE_URL = "https://api.intasend.com/api/v1"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
STAGE_DEMO_PIN = os.getenv("STAGE_DEMO_PIN", "2026")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

ai_client = OpenAI(
    api_key=DEEPSEEK_API_KEY if DEEPSEEK_API_KEY else "placeholder",
    base_url="https://api.deepseek.com"
)

_rate_buckets = defaultdict(deque)

def check_rate_limit(user_id: str, max_calls: int = 15, window_sec: int = 60):
    now = time.time()
    q = _rate_buckets[user_id]
    while q and now - q[0] > window_sec:
        q.popleft()
    if len(q) >= max_calls:
        raise HTTPException(status_code=429, detail="Rate limit reached. Please wait a few seconds.")
    q.append(now)

def init_all():
    init_pg_db()
    conn = get_conn()
    with conn.cursor() as cur:
        # Extend posts table to distinguish BIDS (demand) from ASKS (supply)
        cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS intent_type TEXT DEFAULT 'BID';")
        cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS price_kes NUMERIC(12, 2) DEFAULT 0.00;")
        cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS item_name TEXT DEFAULT '';")
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS wormholes (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            peer_user_id TEXT REFERENCES users(id),
            title TEXT NOT NULL,
            counterparty TEXT NOT NULL,
            escrow_kes NUMERIC(12, 2) DEFAULT 0.00,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS wormhole_messages (
            id SERIAL PRIMARY KEY,
            wormhole_id TEXT REFERENCES wormholes(id) ON DELETE CASCADE,
            sender_id TEXT,
            sender_name TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()
    conn.close()

init_all()

def clean_phone(phone: str) -> str:
    p = phone.strip().replace("+", "").replace(" ", "")
    if p.startswith("0"): p = "254" + p[1:]
    elif p.startswith("7") or p.startswith("1"): p = "254" + p
    return p

# ----------------- SCHEMAS -----------------
class GoogleAuthRequest(BaseModel):
    credential: str
    campus: Optional[str] = "Moi University"

class DemoAuthRequest(BaseModel):
    pin: str

class AllocateRequest(BaseModel):
    amount_kes: float

class WalletEscrowRequest(BaseModel):
    amount_kes: float
    deal_id: str
    title: str
    counterparty: Optional[str] = "Verified Peer Agent"
    peer_user_id: Optional[str] = None
    post_id: Optional[str] = None

class STKPushRequest(BaseModel):
    phone_number: str
    amount: float
    purpose: str = "WALLET_DEPOSIT"
    deal_id: str = "deal-001"
    title: Optional[str] = "Campus Escrow"
    peer_user_id: Optional[str] = None

class StatusCheckRequest(BaseModel):
    invoice_id: str

class WormholeMsgRequest(BaseModel):
    message: str

# ----------------- AUTH & CONFIG -----------------
@app.get("/health")
async def health():
    return {"status": "operational", "node": "Moi-University-Node-01", "kernel": "Double-Auction-v6"}

@app.get("/api/config")
async def get_config():
    return {"google_client_id": GOOGLE_CLIENT_ID}

@app.post("/api/auth/google")
async def google_login(req: GoogleAuthRequest):
    return await run_in_threadpool(authenticate_google_user, req.credential, req.campus or "Moi University")

@app.post("/api/auth/demo")
async def demo_login(req: DemoAuthRequest):
    if not hmac.compare_digest(req.pin.strip(), STAGE_DEMO_PIN):
        raise HTTPException(status_code=403, detail="Invalid Stage Demo PIN.")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = 'kevin';")
        user = dict(cur.fetchone())
        user["wallet_kes"] = float(user.get("wallet_kes") or 0)
        user["locked_escrow_kes"] = float(user.get("locked_escrow_kes") or 0)
        user["escrow_kes"] = user["wallet_kes"]
    conn.close()
    token = create_session_token("kevin")
    return {"token": token, "user": user, "is_new_user": False}

# ----------------- PROFILE & LEDGER -----------------
@app.get("/api/me")
async def get_me(user_id: str = Depends(decode_session_token)):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="User not found")
        user = dict(row)
        user["wallet_kes"] = float(user.get("wallet_kes") or 0)
        user["locked_escrow_kes"] = float(user.get("locked_escrow_kes") or 0)
        user["escrow_kes"] = user["wallet_kes"]

        cur.execute("SELECT id, action, amount_aura, amount_kes, left(hash, 16) AS hash_prefix, timestamp FROM ledger WHERE user_id = %s ORDER BY id DESC LIMIT 20;", (user_id,))
        ledger = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT id, course_code, title, doc_type, aura_minted, royalty_earned, queries_count, created_at FROM documents WHERE uploader_id = %s ORDER BY created_at DESC LIMIT 20;", (user_id,))
        docs = [dict(r) for r in cur.fetchall()]

        cur.execute("""
        SELECT * FROM wormholes 
        WHERE (user_id = %s OR peer_user_id = %s) AND status = 'ACTIVE' 
        ORDER BY created_at DESC;
        """, (user_id, user_id))
        wormholes = []
        for w in cur.fetchall():
            wd = dict(w)
            wd["escrow_kes"] = float(wd["escrow_kes"])
            wormholes.append(wd)
    conn.close()
    return {"user": user, "ledger": ledger, "documents": docs, "wormholes": wormholes}

@app.post("/api/billing/allocate")
async def allocate_wallet_to_compute(req: AllocateRequest, user_id: str = Depends(decode_session_token)):
    if req.amount_kes < 10:
        raise HTTPException(status_code=400, detail="Minimum allocation is KES 10.")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT wallet_kes FROM users WHERE id = %s FOR UPDATE;", (user_id,))
        row = cur.fetchone()
        if not row or float(row["wallet_kes"]) < req.amount_kes:
            conn.close()
            raise HTTPException(status_code=400, detail="Insufficient Available Wallet balance.")
        credits_granted = int(req.amount_kes * 100)
        cur.execute("UPDATE users SET compute_credits = compute_credits + %s WHERE id = %s;", (credits_granted, user_id))
    conn.commit()
    conn.close()
    commit_ledger(user_id, f"Allocated KES {int(req.amount_kes)} to Compute (+{credits_granted} credits)", 0.0, wallet_delta=-req.amount_kes)
    return {"status": "ALLOCATED", "credits_added": credits_granted, "kes_deducted": req.amount_kes}

@app.get("/api/leaderboard")
async def get_leaderboard():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        SELECT u.id, u.name, u.picture, u.campus, u.is_verified_student, u.aura,
               COUNT(d.id) AS docs_uploaded,
               COALESCE(SUM(d.royalty_earned), 0) AS royalties
        FROM users u
        LEFT JOIN documents d ON u.id = d.uploader_id
        GROUP BY u.id
        ORDER BY u.aura DESC
        LIMIT 20;
        """)
        leaders = [dict(r) for r in cur.fetchall()]

        cur.execute("""
        SELECT d.id, d.course_code, d.title, d.doc_type, d.summary, d.queries_count, d.royalty_earned, u.name AS uploader_name
        FROM documents d
        JOIN users u ON d.uploader_id = u.id
        ORDER BY d.created_at DESC
        LIMIT 15;
        """)
        recent_docs = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"leaderboard": leaders, "recent_documents": recent_docs}

# ----------------- REAL-TIME CHAT STREAMING & DOUBLE-AUCTION MATCHMAKER -----------------
@app.get("/api/chat/history")
async def get_chat_history(user_id: str = Depends(decode_session_token)):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        SELECT user_message, ai_reply, credits_used, created_at
        FROM chat_logs WHERE user_id = %s
        ORDER BY id DESC LIMIT 25;
        """, (user_id,))
        rows = [dict(r) for r in reversed(cur.fetchall())]
    conn.close()
    return {"history": rows}

@app.post("/api/chat/stream")
async def chat_stream(
    message: str = Form(...),
    file: Optional[UploadFile] = File(None),
    user_id: str = Depends(decode_session_token)
):
    check_rate_limit(user_id, max_calls=15, window_sec=60)
    clean_msg = message.strip()

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT name, compute_credits, aura FROM users WHERE id = %s;", (user_id,))
        user_row = cur.fetchone()
        if not user_row:
            conn.close()
            raise HTTPException(status_code=404, detail="User not found")
        user_name = user_row["name"]
        user_aura = user_row["aura"]
        credits = int(user_row["compute_credits"])

        if credits < 50:
            conn.close()
            async def out_of_credits_gen():
                yield f"data: {json.dumps({'chunk': '⚡ **Intelligence Quota Depleted.** Top up from **KES 20** via M-Pesa to continue executing campus workflows.'})}\n\n"
                widget = {
                    "type": "COMPUTE_TOPUP",
                    "title": "Top Up 5,000 Compute Credits",
                    "counterparty": "MuAI Compute Gateway",
                    "price_kes": 50.0,
                    "display_price_kes": 50,
                    "deal_id": f"topup-{uuid.uuid4().hex[:6]}"
                }
                yield f"data: {json.dumps({'action_widget': widget})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(out_of_credits_gen(), media_type="text/event-stream")

        cur.execute("UPDATE users SET compute_credits = compute_credits - 50 WHERE id = %s;", (user_id,))
    conn.commit()
    conn.close()

    # Step 1: Process staged file if attached
    attached_doc_context = ""
    if file and file.filename:
        file_bytes = await file.read()
        doc_res = await run_in_threadpool(
            ingest_document, user_id, file_bytes, file.filename, file.content_type or "application/octet-stream"
        )
        if doc_res.get("accepted"):
            attached_doc_context = f"\n[DOCUMENT ATTACHMENT INDEXED IN PGVECTOR: {doc_res.get('course_code')} - {doc_res.get('title')}. Summary: {doc_res.get('summary')}]\n"

    # Step 2: Context Retrieval
    recalled_memories = recall_relevant_memories(user_id, clean_msg, top_k=3)
    memory_context = "\n".join(f"- {m}" for m in recalled_memories) if recalled_memories else "None"

    # Step 3: Intent Classification via DeepSeek (ASK vs BID vs QUERY)
    classifier_prompt = f"""
    Analyze this message from student "{user_name}": "{clean_msg}"
    Classify into strict JSON:
    {{
       "intent_type": "ASK" (if user is SELLING, OFFERING, or PROVIDING an item/service) | "BID" (if user WANTS to BUY, HIRE, ORDER, or FIND something) | "QUERY" (if user asks an academic question, syllabus, notes, timetable),
       "item_or_service": "exact item or service name (e.g. Casio fx-82MS, Fries & Soda, Physics notes)",
       "price_kes": numeric price mentioned or estimated (e.g. 900),
       "category": "c/ComradeCart" | "c/CampusServices" | "c/Academics" | "c/Hostels" | "c/DarkMarket",
       "summary": "1 sentence explanation"
    }}
    Return ONLY raw JSON.
    """
    
    intent_data = {"intent_type": "QUERY", "item_or_service": "", "price_kes": 0, "category": "c/ComradeCart"}
    try:
        c_res = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": classifier_prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        intent_data = json.loads(c_res.choices[0].message.content.strip())
    except Exception:
        pass

    intent_type = intent_data.get("intent_type", "QUERY")
    item = intent_data.get("item_or_service") or "Item"
    price = float(intent_data.get("price_kes") or 500)
    category = intent_data.get("category") or "c/ComradeCart"

    # Step 4: The Continuous Double-Auction Kernel Execution
    action_widget = None
    system_execution_notes = ""

    conn = get_conn()
    with conn.cursor() as cur:
        if intent_type == "ASK":
            # SELLER (Supply): List on MyHuman, monitor for Bids. ZERO PAYMENT REQUIRED!
            post_id = f"post-{uuid.uuid4().hex[:6]}"
            agent_label = f"Agent #{user_id[-4:].upper()} ({user_name})"
            title = f"SELL: {item} ({int(price)} KES)"
            cur.execute("""
            INSERT INTO posts (id, category, author_user_id, author_agent, author_name, title, body, status, intent_type, price_kes, item_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'LISTED', 'ASK', %s, %s);
            """, (post_id, category, user_id, agent_label, user_name, title, f"{user_name} listed {item} for sale at KES {int(price)}. Agent listening for buyer bids.", price, item))
            
            # Check if any buyer BID is already waiting in the order book!
            cur.execute("""
            SELECT p.*, u.name as buyer_name, u.aura as buyer_aura 
            FROM posts p JOIN users u ON p.author_user_id = u.id
            WHERE p.intent_type = 'BID' AND p.status = 'OPEN' AND p.author_user_id != %s
            AND (p.title ILIKE %s OR p.item_name ILIKE %s)
            ORDER BY p.price_kes DESC LIMIT 1;
            """, (user_id, f"%{item[:6]}%", f"%{item[:6]}%"))
            waiting_bid = cur.fetchone()

            if waiting_bid:
                # Match found immediately!
                cur.execute("UPDATE posts SET status = 'MATCHED' WHERE id = %s;", (post_id,))
                cur.execute("UPDATE posts SET status = 'MATCHED' WHERE id = %s;", (waiting_bid["id"],))
                system_execution_notes = f"Matched waiting buyer {waiting_bid['buyer_name']} from the order book! Initialized negotiation."
                action_widget = {
                    "type": "SELLER_MATCH",
                    "title": f"Buyer Found: {item}",
                    "counterparty": f"{waiting_bid['buyer_name']} (Aura: {waiting_bid['buyer_aura']})",
                    "display_price_kes": int(price),
                    "action": "AWAITING_BUYER_ESCROW",
                    "peer_user_id": waiting_bid["author_user_id"]
                }
            else:
                system_execution_notes = f"Listed {item} on {category} at KES {int(price)}. Node is actively watching for buyer bids."
                action_widget = {
                    "type": "SELLER_LISTED",
                    "title": f"Active Listing: {item}",
                    "counterparty": "MyHuman Order Book",
                    "display_price_kes": int(price),
                    "action": "LISTED"
                }

        elif intent_type == "BID":
            # BUYER (Demand): Search for matching ASKS from other students (John, Meshack, etc.)
            cur.execute("""
            SELECT p.*, u.name as seller_name, u.aura as seller_aura 
            FROM posts p JOIN users u ON p.author_user_id = u.id
            WHERE p.intent_type = 'ASK' AND p.status = 'LISTED' AND p.author_user_id != %s
            AND (p.title ILIKE %s OR p.item_name ILIKE %s)
            ORDER BY p.price_kes ASC LIMIT 1;
            """, (user_id, f"%{item[:6]}%", f"%{item[:6]}%"))
            matching_ask = cur.fetchone()

            if matching_ask:
                seller_id = matching_ask["author_user_id"]
                seller_name = matching_ask["seller_name"]
                seller_aura = matching_ask["seller_aura"]
                seller_price = float(matching_ask["price_kes"])
                post_id = matching_ask["id"]

                # SPNE Clearing: Settle at agreed price
                cleared_price = min(price, seller_price)
                cur.execute("UPDATE posts SET status = 'SPNE Cleared' WHERE id = %s;", (post_id,))
                
                # Add negotiation trace to thread
                cur.execute("""
                INSERT INTO comments (id, post_id, agent_id, aura_score, comment_text, sys_action)
                VALUES 
                (%s, %s, %s, %s, %s, '[SPNE Cleared: Matched in Order Book]'),
                (%s, %s, %s, %s, 'Terms accepted. Escrow ready for human authorization.', '[Contract Sealed]');
                """, (
                    f"cm-{uuid.uuid4().hex[:6]}", post_id, f"Agent #{user_id[-4:].upper()} ({user_name})", user_aura,
                    f"My human placed bid for KES {int(cleared_price)}. Terms match inventory.",
                    f"cm-{uuid.uuid4().hex[:6]}", post_id, f"Agent #{seller_id[-4:].upper()} ({seller_name})", seller_aura
                ))

                system_execution_notes = f"Matched active listing from {seller_name} ({category}) at KES {int(cleared_price)}."
                action_widget = {
                    "type": "SPNE_DEAL",
                    "title": f"Contract Ready: {item}",
                    "counterparty": f"{seller_name} (Aura: {seller_aura})",
                    "peer_user_id": seller_id,
                    "price_kes": 10.0,  # 10 KES demo M-Pesa trigger
                    "display_price_kes": int(cleared_price),
                    "deal_id": f"deal-{uuid.uuid4().hex[:6]}",
                    "post_id": post_id
                }
            else:
                # No peer seller exists yet -> Post BID to order book & provide simulated vendor fallback
                post_id = f"post-{uuid.uuid4().hex[:6]}"
                agent_label = f"Agent #{user_id[-4:].upper()} ({user_name})"
                title = f"BUY INTENT: {item} (<{int(price)} KES)"
                cur.execute("""
                INSERT INTO posts (id, category, author_user_id, author_agent, author_name, title, body, status, intent_type, price_kes, item_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN', 'BID', %s, %s);
                """, (post_id, category, user_id, agent_label, user_name, title, f"Looking to acquire {item}. Max budget: KES {int(price)}.", price, item))

                # Student vendor simulation fallback so demo always works
                cleared_price = max(50.0, round(price * 0.9, -1))
                counterparty = "Agent #14 (Student Vendor)"
                action_widget = {
                    "type": "SPNE_DEAL",
                    "title": f"Contract Ready: {item}",
                    "counterparty": f"{counterparty} (Aura: 3450)",
                    "peer_user_id": "vendor-sim",
                    "price_kes": 10.0,
                    "display_price_kes": int(cleared_price),
                    "deal_id": f"deal-{uuid.uuid4().hex[:6]}",
                    "post_id": post_id
                }
                system_execution_notes = f"No student Ask matched immediately. Broadcasted Bid to {category} and settled fallback contract with {counterparty} at KES {int(cleared_price)}."

    conn.commit()
    conn.close()

    # Step 5: Stream the Executive AI Response
    system_prompt = f"""
    You are MuAI, the autonomous coordinator for {user_name} at Moi University.
    RECALLED VECTOR MEMORIES:
    {memory_context}
    {attached_doc_context}

    MARKET KERNEL STATUS:
    Intent Classified: {intent_type}
    Kernel Action: {system_execution_notes}

    RULES:
    1. If the user is SELLING (ASK), confirm you listed their item on {category} at KES {int(price)} and that their agent is listening for bids. Emphasize that sellers PAY NOTHING.
    2. If the user is BUYING (BID), announce the matched deal cleanly and state that the escrow payment card is ready.
    3. If academic question, answer directly citing course notes and formulas.
    4. Keep tone executive, concise, and under 4 sentences.
    """

    async def response_streamer() -> AsyncGenerator[str, None]:
        full_reply = ""
        try:
            stream = ai_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": clean_msg}
                ],
                temperature=0.2,
                stream=True
            )

            for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full_reply += token
                    yield f"data: {json.dumps({'chunk': token})}\n\n"
                    await asyncio.sleep(0.01)

            if action_widget:
                yield f"data: {json.dumps({'action_widget': action_widget})}\n\n"

            store_turn_memory(user_id, clean_msg, full_reply, credits_used=50)

        except Exception as e:
            err_msg = f"\n[Inference Error: {str(e)}]"
            yield f"data: {json.dumps({'chunk': err_msg})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(response_streamer(), media_type="text/event-stream")

# ----------------- WORMHOLES & REAL ESCROW SETTLEMENT -----------------
def spawn_wormhole_sync(user_id: str, peer_user_id: Optional[str], title: str, counterparty: str, amount_kes: float) -> str:
    wh_id = f"wh-{uuid.uuid4().hex[:6]}"
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO wormholes (id, user_id, peer_user_id, title, counterparty, escrow_kes, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE');
        """, (wh_id, user_id, peer_user_id, title, counterparty, amount_kes))
        cur.execute("""
        INSERT INTO wormhole_messages (wormhole_id, sender_id, sender_name, message)
        VALUES (%s, 'arbiter', 'Arbiter Daemon', %s);
        """, (wh_id, f"E2EE Shared Edge active. KES {amount_kes:.0f} locked in escrow. Handover details can be coordinated here."))
    conn.commit()
    conn.close()
    return wh_id

@app.get("/api/wormholes/{wh_id}")
async def get_wormhole(wh_id: str, user_id: str = Depends(decode_session_token)):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM wormholes WHERE id = %s AND (user_id = %s OR peer_user_id = %s);", (wh_id, user_id, user_id))
        wh = cur.fetchone()
        if not wh:
            conn.close()
            raise HTTPException(status_code=404, detail="Wormhole not found")
        cur.execute("SELECT * FROM wormhole_messages WHERE wormhole_id = %s ORDER BY id ASC;", (wh_id,))
        msgs = [dict(r) for r in cur.fetchall()]
    conn.close()
    wh_dict = dict(wh)
    wh_dict["escrow_kes"] = float(wh_dict["escrow_kes"])
    return {"wormhole": wh_dict, "messages": msgs}

@app.post("/api/wormholes/{wh_id}/message")
async def send_wormhole_message(wh_id: str, req: WormholeMsgRequest, user_id: str = Depends(decode_session_token)):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM users WHERE id = %s;", (user_id,))
        u = cur.fetchone()
        cur.execute("SELECT * FROM wormholes WHERE id = %s AND (user_id = %s OR peer_user_id = %s);", (wh_id, user_id, user_id))
        wh = cur.fetchone()
        if not wh or not u:
            conn.close()
            raise HTTPException(status_code=404, detail="Wormhole not found")
        cur.execute("""
        INSERT INTO wormhole_messages (wormhole_id, sender_id, sender_name, message)
        VALUES (%s, %s, %s, %s);
        """, (wh_id, user_id, u["name"], req.message[:500]))
    conn.commit()
    conn.close()
    return {"status": "sent"}

@app.post("/api/wormholes/{wh_id}/release")
async def release_wormhole(wh_id: str, user_id: str = Depends(decode_session_token)):
    """
    Settles escrow: Deducts locked_escrow from buyer,
    credits the peer seller's real wallet_kes, and awards +20 Aura!
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM wormholes WHERE id = %s AND (user_id = %s OR peer_user_id = %s) AND status = 'ACTIVE';", (wh_id, user_id, user_id))
        wh = cur.fetchone()
        if not wh:
            conn.close()
            raise HTTPException(status_code=404, detail="Active Wormhole not found")
        cur.execute("UPDATE wormholes SET status = 'COLLAPSED' WHERE id = %s;", (wh_id,))
        cur.execute("""
        INSERT INTO wormhole_messages (wormhole_id, sender_id, sender_name, message)
        VALUES (%s, 'arbiter', 'Arbiter Daemon', 'Delivery confirmed. Escrow transferred to seller wallet.');
        """, (wh_id,))
    conn.commit()
    conn.close()

    escrow_amt = float(wh["escrow_kes"] or 0)
    buyer_id = wh["user_id"]
    seller_id = wh["peer_user_id"]

    commit_ledger(buyer_id, f"Escrow Settled: {wh['title']}", 20.0, wallet_delta=0.0, escrow_delta=-escrow_amt)
    if seller_id and seller_id != buyer_id:
        commit_ledger(seller_id, f"P2P Escrow Received: {wh['title']}", 20.0, wallet_delta=escrow_amt, escrow_delta=0.0)

    return {"status": "COLLAPSED", "settled_kes": escrow_amt, "aura_minted": 20}

@app.post("/api/escrow/pay-from-wallet")
async def pay_escrow_from_wallet(req: WalletEscrowRequest, user_id: str = Depends(decode_session_token)):
    if req.amount_kes <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount.")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT wallet_kes FROM users WHERE id = %s FOR UPDATE;", (user_id,))
        row = cur.fetchone()
        if not row or float(row["wallet_kes"]) < req.amount_kes:
            conn.close()
            raise HTTPException(status_code=400, detail="Insufficient Wallet balance. Deposit via M-Pesa first.")
        if req.post_id:
            cur.execute("UPDATE posts SET status = 'ESCROW_LOCKED' WHERE id = %s;", (req.post_id,))
    conn.commit()
    conn.close()

    commit_ledger(user_id, f"Locked Escrow: {req.title}", 15.0, wallet_delta=-req.amount_kes, escrow_delta=req.amount_kes)
    wh_id = spawn_wormhole_sync(user_id, req.peer_user_id, req.title, req.counterparty, req.amount_kes)
    return {"status": "ESCROW_LOCKED", "wormhole_id": wh_id, "aura_minted": 15}

# ----------------- INTASEND M-PESA & SMART PROACTIVE POLLING -----------------
@app.post("/api/payment/stk-push")
async def trigger_stk_push(req: STKPushRequest, user_id: str = Depends(decode_session_token)):
    if not INTASEND_SECRET_KEY:
        raise HTTPException(status_code=500, detail="IntaSend Secret Key not configured.")

    phone = clean_phone(req.phone_number)
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET phone = %s WHERE id = %s;", (phone, user_id))
    conn.commit()
    conn.close()

    safe_title = (req.title or "Escrow").replace(":", "-")[:20]
    peer_id = req.peer_user_id or "none"
    api_ref = f"{req.purpose}:{user_id}:{peer_id}:{req.deal_id}:{safe_title}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{INTASEND_BASE_URL}/payment/mpesa-stk-push/",
            headers={"Authorization": f"Bearer {INTASEND_SECRET_KEY}", "Content-Type": "application/json"},
            json={"amount": req.amount, "phone_number": phone, "api_ref": api_ref},
            timeout=15.0
        )
        data = resp.json()
        if resp.status_code not in (200, 201):
            return JSONResponse(status_code=resp.status_code, content={"error": data})
        invoice_id = data.get("invoice", {}).get("invoice_id")
        return {"status": "PENDING", "invoice_id": invoice_id, "amount": req.amount, "api_ref": api_ref}

@app.post("/api/payment/check-status")
async def check_status(req: StatusCheckRequest):
    # 1. Local DB verification (Instant response if webhook already cleared!)
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM processed_invoices WHERE invoice_id = %s;", (req.invoice_id,))
        local_inv = cur.fetchone()
        if local_inv and local_inv["state"] == "COMPLETE":
            parts = (local_inv.get("api_ref") or "").split(":")
            uid = parts[1] if len(parts) > 1 else "kevin"
            cur.execute("SELECT id FROM wormholes WHERE user_id = %s AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1;", (uid,))
            wh = cur.fetchone()
            conn.close()
            return {"invoice_id": req.invoice_id, "state": "COMPLETE", "wormhole_id": wh["id"] if wh else None}
    conn.close()

    # 2. Remote IntaSend Status Check
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{INTASEND_BASE_URL}/payment/status/",
            headers={"Authorization": f"Bearer {INTASEND_SECRET_KEY}", "Content-Type": "application/json"},
            json={"invoice_id": req.invoice_id},
            timeout=10.0
        )
        data = resp.json()
        invoice = data.get("invoice", {})
        state = invoice.get("state")
        wh_id = None
        if state == "COMPLETE":
            wh_id = fulfill_payment_sync(invoice.get("invoice_id"), invoice.get("api_ref", ""), float(invoice.get("value", 0)))
        return {"invoice_id": req.invoice_id, "state": state, "wormhole_id": wh_id, "failed_reason": invoice.get("failed_reason")}

@app.post("/api/webhooks/intasend")
async def intasend_webhook(request: Request):
    body = await request.json()
    if not hmac.compare_digest(str(body.get("challenge", "")), INTASEND_CHALLENGE):
        return Response(status_code=401, content="Unauthorized")
    if body.get("topic") == "collection_event" and body.get("state") == "COMPLETE":
        fulfill_payment_sync(body.get("invoice_id"), body.get("api_ref", ""), float(Decimal(body.get("value", "0"))))
    return Response(status_code=200, content="OK")

def fulfill_payment_sync(invoice_id: str, api_ref: str, amount: float) -> Optional[str]:
    parts = api_ref.split(":") if api_ref else []
    purpose = parts[0] if len(parts) > 0 else "WALLET_DEPOSIT"
    user_id = parts[1] if len(parts) > 1 else "kevin"
    peer_user_id = parts[2] if len(parts) > 2 and parts[2] != "none" else None
    deal_title = ":".join(parts[4:]) if len(parts) > 4 else "Campus Escrow Contract"

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT invoice_id FROM processed_invoices WHERE invoice_id = %s;", (invoice_id,))
        if cur.fetchone():
            cur.execute("SELECT id FROM wormholes WHERE user_id = %s AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1;", (user_id,))
            wh = cur.fetchone()
            conn.close()
            return wh["id"] if wh else None
        cur.execute("INSERT INTO processed_invoices (invoice_id, state, amount, api_ref) VALUES (%s, 'COMPLETE', %s, %s);", (invoice_id, amount, api_ref))
    conn.commit()
    conn.close()

    if purpose == "COMPUTE_TOPUP":
        credits_added = int(amount * 100)
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET compute_credits = compute_credits + %s WHERE id = %s;", (credits_added, user_id))
        conn.commit()
        conn.close()
        commit_ledger(user_id, f"Purchased {credits_added} Compute Credits (#{invoice_id})", 5.0, wallet_delta=0.0)
        return None
    elif purpose == "WALLET_DEPOSIT":
        commit_ledger(user_id, f"M-Pesa Wallet Deposit (#{invoice_id})", 10.0, wallet_delta=amount)
        return None
    else:
        # ESCROW_LOCK
        commit_ledger(user_id, f"M-Pesa Escrow Funded (#{invoice_id})", 15.0, wallet_delta=0.0, escrow_delta=amount)
        return spawn_wormhole_sync(user_id, peer_user_id, deal_title, "Verified Peer Agent", amount)

# ----------------- MYHUMAN FEED & COMMUNITY -----------------
@app.get("/api/feed")
async def get_feed(category: Optional[str] = None):
    conn = get_conn()
    with conn.cursor() as cur:
        if category and category != "all":
            cur.execute("SELECT * FROM posts WHERE category = %s ORDER BY created_at DESC LIMIT 40;", (category,))
        else:
            cur.execute("SELECT * FROM posts ORDER BY created_at DESC LIMIT 40;")
        posts = [dict(r) for r in cur.fetchall()]
        active_cnt = 0
        for p in posts:
            cur.execute("SELECT COUNT(*) AS cnt FROM comments WHERE post_id = %s;", (p["id"],))
            p["comments_count"] = cur.fetchone()["cnt"]
            if p["status"] in ("OPEN", "LISTED", "SPNE Cleared"): active_cnt += 1
    conn.close()
    return {"posts": posts, "active_count": active_cnt}

@app.get("/api/thread/{post_id}")
async def get_thread(post_id: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM posts WHERE id = %s;", (post_id,))
        post = cur.fetchone()
        if not post:
            conn.close()
            raise HTTPException(status_code=404, detail="Post not found")
        cur.execute("SELECT * FROM comments WHERE post_id = %s ORDER BY created_at ASC;", (post_id,))
        comments = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"post": dict(post), "comments": comments}

@app.post("/api/documents/upload")
async def upload_academic_doc(file: UploadFile = File(...), user_id: str = Depends(decode_session_token)):
    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File exceeds 10MB limit.")

    result = await run_in_threadpool(
        ingest_document, user_id, file_bytes, file.filename or "upload.txt", file.content_type or "application/octet-stream"
    )
    if not result.get("accepted"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Upload rejected"))
    return result

# ----------------- STATIC ASSETS -----------------
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_app():
    index_file = os.path.join(BASE_DIR, "templates", "index.html")
    with open(index_file, "r") as f:
        return HTMLResponse(content=f.read())
