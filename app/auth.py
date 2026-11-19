import os
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import HTTPException, Header
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from dotenv import load_dotenv

from app.db import get_conn, commit_ledger

load_dotenv("/opt/muai-engine/.env")
load_dotenv(".env")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
JWT_SECRET = os.getenv("JWT_SECRET", "muai-super-secret-jwt-key-2026")
JWT_ALGORITHM = "HS256"

def create_session_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "iat": datetime.now(timezone.utc)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_session_token(authorization: str = Header(None)) -> str:
    """Extracts user_id from Bearer JWT. Falls back cleanly if invalid."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token.")

def authenticate_google_user(credential: str, campus: str = "Moi University") -> dict:
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured on server.")

    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google credential: {str(e)}")

    user_id = f"g-{idinfo['sub']}"
    email = idinfo.get("email", "")
    name = idinfo.get("name", email.split("@")[0])
    picture = idinfo.get("picture", "")
    is_student = email.lower().endswith(".ac.ke") or email.lower().endswith(".edu")

    conn = get_conn()
    is_new_user = False
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
        existing = cur.fetchone()
        if not existing:
            is_new_user = True
            cur.execute("""
            INSERT INTO users (id, email, name, picture, is_verified_student, campus, aura, battery, escrow_kes)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 100, 0.00)
            RETURNING *;
            """, (user_id, email, name, picture, is_student, campus))
            user_row = dict(cur.fetchone())
        else:
            cur.execute("""
            UPDATE users SET name = %s, picture = %s WHERE id = %s RETURNING *;
            """, (name, picture, user_id))
            user_row = dict(cur.fetchone())
    conn.commit()
    conn.close()

    if is_new_user:
        commit_ledger(user_id, "Node Genesis: Initialized on Campus Grid", 100.0, 0.0)
        user_row["aura"] = 100

    token = create_session_token(user_id)
    return {"token": token, "user": user_row, "is_new_user": is_new_user}
