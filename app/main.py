import os
import hmac
import json
import sqlite3
import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv

# Load Environment Variables
load_dotenv("/opt/muai-engine/.env")
load_dotenv(".env")

app = FastAPI(title="MuAI Sovereign Epistemic Engine", version="1.0.0")

DB_PATH = "/opt/muai-engine/muai.db" if os.path.exists("/opt/muai-engine") else "muai.db"

# ----------------- INTASEND & DEEPSEEK CONFIG -----------------
INTASEND_SECRET_KEY = os.getenv("INTASEND_SECRET_KEY", "")
INTASEND_PUB_KEY = os.getenv("INTASEND_PUBLISHABLE_KEY", "")
INTASEND_CHALLENGE = os.getenv("INTASEND_WEBHOOK_CHALLENGE", "muai-hackathon-challenge-2026")
INTASEND_BASE_URL = "https://api.intasend.com/api/v1"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
ai_client = OpenAI(
    api_key=DEEPSEEK_API_KEY if DEEPSEEK_API_KEY else "placeholder",
    base_url="https://api.deepseek.com"
)

# ----------------- DATABASE SETUP -----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        name TEXT,
        phone TEXT,
        aura INTEGER DEFAULT 4310,
        battery INTEGER DEFAULT 85,
        escrow_kes REAL DEFAULT 3850.0,
        compute_credits INTEGER DEFAULT 5000
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS processed_invoices (
        invoice_id TEXT PRIMARY KEY,
        state TEXT,
        amount REAL,
        api_ref TEXT,
        account TEXT,
        updated_at DATETIME
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        action TEXT,
        amount_aura REAL,
        amount_kes REAL,
        prev_hash TEXT,
        hash TEXT,
        timestamp DATETIME
    )""")
    cursor.execute("INSERT OR IGNORE INTO users (id, name, phone, aura, battery, escrow_kes, compute_credits) VALUES ('kevin', 'Kevin M.', '254712345678', 4310, 85, 3850.0, 5000)")
    conn.commit()
    conn.close()

init_db()

# ----------------- UTILITIES -----------------
def clean_phone(phone: str) -> str:
    """Formats phone to 2547XXXXXXXX or 2541XXXXXXXX as required by IntaSend"""
    p = phone.strip().replace("+", "").replace(" ", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    elif p.startswith("7") or p.startswith("1"):
        p = "254" + p
    return p

def commit_ledger_entry(user_id: str, action: str, amount_aura: float, amount_kes: float) -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT hash FROM ledger ORDER BY id DESC LIMIT 1")
    last_row = cursor.fetchone()
    prev_hash = last_row["hash"] if last_row else "0" * 64

    timestamp = datetime.utcnow().isoformat()
    raw = f"{prev_hash}|{user_id}|{action}|{amount_aura}|{amount_kes}|{timestamp}"
    new_hash = hashlib.sha256(raw.encode()).hexdigest()

    cursor.execute("""
    INSERT INTO ledger (user_id, action, amount_aura, amount_kes, prev_hash, hash, timestamp)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, action, amount_aura, amount_kes, prev_hash, new_hash, timestamp))

    if amount_aura != 0:
        cursor.execute("UPDATE users SET aura = aura + ? WHERE id = ?", (amount_aura, user_id))
    if amount_kes != 0:
        cursor.execute("UPDATE users SET escrow_kes = escrow_kes + ? WHERE id = ?", (amount_kes, user_id))

    conn.commit()
    conn.close()
    return new_hash

# ----------------- SCHEMAS -----------------
class STKPushRequest(BaseModel):
    user_id: str = "kevin"
    phone_number: str
    amount: float
    purpose: str  # "ESCROW_LOCK" or "COMPUTE_TOPUP"
    deal_id: Optional[str] = "deal-001"

class StatusCheckRequest(BaseModel):
    invoice_id: str

class ChatRequest(BaseModel):
    user_id: str = "kevin"
    message: str

# ----------------- ROUTES -----------------
@app.get("/health")
async def health():
    return {"status": "operational", "node": "Moi-University-Node-01", "port": 8082}

@app.get("/api/state/{user_id}")
async def get_state(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)

# 1. TRIGGER M-PESA STK PUSH (Both Escrow & Subscriptions)
@app.post("/api/payment/stk-push")
async def trigger_stk_push(req: STKPushRequest):
    if not INTASEND_SECRET_KEY:
        raise HTTPException(status_code=500, detail="IntaSend Secret Key not configured on Vultr.")

    sanitized_phone = clean_phone(req.phone_number)
    api_ref = f"{req.purpose}:{req.user_id}:{req.deal_id}"

    payload = {
        "amount": req.amount,
        "phone_number": sanitized_phone,
        "api_ref": api_ref
    }
    headers = {
        "Authorization": f"Bearer {INTASEND_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{INTASEND_BASE_URL}/payment/mpesa-stk-push/",
                headers=headers,
                json=payload,
                timeout=15.0
            )
            data = resp.json()
            if resp.status_code not in [200, 201]:
                return JSONResponse(status_code=resp.status_code, content={"error": data})
            
            invoice_id = data.get("invoice", {}).get("invoice_id")
            return {
                "status": "PENDING",
                "invoice_id": invoice_id,
                "customer": sanitized_phone,
                "amount": req.amount,
                "api_ref": api_ref
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Transport failure: {str(e)}")

# 2. INTASEND STATUS CHECK (Polling Fallback)
@app.post("/api/payment/check-status")
async def check_status(req: StatusCheckRequest):
    headers = {
        "Authorization": f"Bearer {INTASEND_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{INTASEND_BASE_URL}/payment/status/",
                headers=headers,
                json={"invoice_id": req.invoice_id},
                timeout=10.0
            )
            data = resp.json()
            invoice = data.get("invoice", {})
            state = invoice.get("state")

            if state == "COMPLETE":
                await fulfill_payment(invoice.get("invoice_id"), invoice.get("api_ref"), float(invoice.get("value", 0)))

            return {"invoice_id": req.invoice_id, "state": state, "details": invoice}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

# 3. LIVE WEBHOOK HANDLER (With Challenge & Idempotency)
@app.post("/api/webhooks/intasend")
async def intasend_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="Invalid JSON")

    # A. Verify Challenge
    incoming_challenge = str(body.get("challenge", ""))
    if not hmac.compare_digest(incoming_challenge, INTASEND_CHALLENGE):
        return Response(status_code=401, content="Invalid Webhook Challenge")

    # B. Filter Collection Event
    topic = body.get("topic")
    state = body.get("state")
    invoice_id = body.get("invoice_id")
    api_ref = body.get("api_ref", "")
    value_str = body.get("value", "0.00")

    if topic == "collection_event" and state == "COMPLETE":
        amount = float(Decimal(value_str))
        await fulfill_payment(invoice_id, api_ref, amount)

    return Response(status_code=200, content="OK")

async def fulfill_payment(invoice_id: str, api_ref: str, amount: float):
    """Idempotently commits payment and mints Aura"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT invoice_id FROM processed_invoices WHERE invoice_id = ?", (invoice_id,))
    if cursor.fetchone():
        conn.close()
        return  # Already fulfilled

    cursor.execute("""
    INSERT INTO processed_invoices (invoice_id, state, amount, api_ref, updated_at)
    VALUES (?, 'COMPLETE', ?, ?, ?)
    """, (invoice_id, amount, api_ref, datetime.utcnow()))
    conn.commit()
    conn.close()

    # Parse api_ref: e.g. "ESCROW_LOCK:kevin:deal-001" or "COMPUTE_TOPUP:kevin:50000"
    parts = api_ref.split(":") if api_ref else []
    purpose = parts[0] if len(parts) > 0 else "UNKNOWN"
    user_id = parts[1] if len(parts) > 1 else "kevin"

    if purpose == "ESCROW_LOCK":
        # Commit Escrow and award +15 Aura for contract fulfillment
        commit_ledger_entry(user_id, f"Escrow Funded (Invoice {invoice_id})", 15.0, amount)
    elif purpose == "COMPUTE_TOPUP":
        # Add credits (e.g., 50,000 tokens)
        conn = get_db()
        conn.cursor().execute("UPDATE users SET compute_credits = compute_credits + 50000 WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()
        commit_ledger_entry(user_id, f"Compute Credits Topup (Invoice {invoice_id})", 5.0, 0.0)

# 4. DEEPSEEK REASONING ENGINE
@app.post("/api/chat")
async def chat(req: ChatRequest):
    if not DEEPSEEK_API_KEY:
        return {"reply": "DeepSeek API Key not set on Vultr.", "action_widget": None}

    system_prompt = """
    You are MuAI, an autonomous agent operating on behalf of a university student at Moi University.
    Extract the human intent concisely. If the user wants to buy, sell, tutor, hire, or request services,
    outline an actionable plan with exact prices in KES. Keep responses punchy and executive.
    """
    try:
        response = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": req.message}
            ],
            temperature=0.3
        )
        ai_reply = response.choices[0].message.content

        # Interactive Widget Generator
        action_widget = None
        lower_msg = req.message.lower()
        if "calculator" in lower_msg or "casio" in lower_msg:
            action_widget = {
                "type": "SPNE_DEAL",
                "title": "🤝 Deal Cleared: Casio fx-82MS",
                "counterparty": "Student X (Hostel H • Aura: 4,500)",
                "price_kes": 10.0,  # Set to 10 KES for live demo testing
                "action": "LOCK_ESCROW",
                "deal_id": "calc-88"
            }
        elif "compute" in lower_msg or "credits" in lower_msg or "subscription" in lower_msg:
            action_widget = {
                "type": "COMPUTE_TOPUP",
                "title": "⚡ Top Up 50,000 Compute Credits",
                "counterparty": "MuAI Inference Gateway",
                "price_kes": 10.0,
                "action": "BUY_CREDITS",
                "deal_id": "topup-50k"
            }

        return {"reply": ai_reply, "action_widget": action_widget}
    except Exception as e:
        return {"reply": f"DeepSeek Engine Notice: {str(e)}", "action_widget": None}

# 5. SERVE FRONTEND AT ROOT
@app.get("/", response_class=HTMLResponse)
async def serve_app():
    index_file = "app/templates/index.html"
    if not os.path.exists(index_file):
        return "<h1>MuAI Web Engine running. index.html loading...</h1>"
    with open(index_file, "r") as f:
        return HTMLResponse(content=f.read())

# Mount static folder
app.mount("/static", StaticFiles(directory="app/static"), name="static")
