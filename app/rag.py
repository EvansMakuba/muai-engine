import os
import io
import json
import uuid
from typing import List, Dict, Any
from pypdf import PdfReader
from google import genai
from google.genai import types
from openai import OpenAI
from dotenv import load_dotenv

from app.db import get_conn, commit_ledger

load_dotenv("/opt/muai-engine/.env")
load_dotenv(".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

def get_genai_client():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured in .env")
    return genai.Client(api_key=GEMINI_API_KEY)

def get_deepseek_client():
    if not DEEPSEEK_API_KEY:
        return None
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

def embed_text(text: str) -> List[float]:
    """Generates a 768-dim embedding using Gemini Embedding API."""
    client = get_genai_client()
    last_err = None
    for model_name in ["gemini-embedding-001", "text-embedding-004"]:
        try:
            res = client.models.embed_content(
                model=model_name,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=768)
            )
            return list(res.embeddings[0].values)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All embedding models failed: {last_err}")

def vec_to_pg(vec: List[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

def chunk_text(text: str, chunk_size: int = 850, overlap: int = 150) -> List[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

def _gemini_vision_ocr(file_bytes: bytes, mime_type: str, prompt: str) -> str:
    client = get_genai_client()
    last_err = None
    for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), prompt]
            )
            return (resp.text or "").strip()
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Vision OCR unavailable: {last_err}")

def extract_text_from_upload(file_bytes: bytes, filename: str, content_type: str) -> str:
    """Extracts text from PDF, images, or plain text using pypdf + Gemini Vision OCR."""
    lower = filename.lower()
    extracted = ""

    if lower.endswith(".pdf") or content_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            extracted = "\n".join(pages).strip()
        except Exception:
            extracted = ""

        if len(extracted) < 80:
            extracted = _gemini_vision_ocr(
                file_bytes,
                "application/pdf",
                "Transcribe all academic text, questions, formulas, and notes from this document accurately."
            )

    elif any(lower.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]) or content_type.startswith("image/"):
        mime = content_type if content_type.startswith("image/") else "image/jpeg"
        extracted = _gemini_vision_ocr(
            file_bytes,
            mime,
            "Transcribe all academic text, exam questions, equations, and handwritten notes from this image accurately."
        )
    else:
        extracted = file_bytes.decode("utf-8", errors="ignore").strip()

    return extracted

def verify_academic_content(text_sample: str, filename: str) -> Dict[str, Any]:
    """Verifies upload is genuine academic material using Gemini with DeepSeek fallback."""
    prompt = f"""
    Inspect this uploaded university document snippet (filename: {filename}) and return ONLY raw JSON:
    {{
        "is_academic": true or false (false ONLY if completely empty, gibberish, or unrelated spam/selfie),
        "course_code": "e.g. PHY 211, CSC 314, ENG 202, or GENERAL",
        "doc_type": "PAST_PAPER" or "LECTURE_NOTES" or "STUDY_GUIDE",
        "title": "Clean concise document title",
        "summary": "2-sentence summary of topics covered",
        "rejection_reason": "short reason if is_academic is false, else null"
    }}

    Document snippet:
    {text_sample[:3000]}
    """
    # Try Gemini models first
    if GEMINI_API_KEY:
        client = get_genai_client()
        for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]:
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
                )
                raw = (resp.text or "{}").strip().replace("```json", "").replace("```", "")
                return json.loads(raw)
            except Exception:
                continue

    # Automatic Fallback to DeepSeek V3
    ds = get_deepseek_client()
    if ds:
        resp = ds.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        return json.loads(raw)

    raise RuntimeError("Both Gemini and DeepSeek verification failed.")

def ingest_document(uploader_id: str, file_bytes: bytes, filename: str, content_type: str) -> Dict[str, Any]:
    """Full Seeding Pipeline: Extract -> Verify -> Embed into pgvector -> Mint +25 Aura."""
    text = extract_text_from_upload(file_bytes, filename, content_type)
    if len(text) < 40:
        return {"accepted": False, "reason": "Could not extract readable academic text from this file."}

    meta = verify_academic_content(text, filename)
    if not meta.get("is_academic", False):
        return {"accepted": False, "reason": meta.get("rejection_reason") or "Document did not pass academic verification."}

    chunks = chunk_text(text)
    if not chunks:
        return {"accepted": False, "reason": "Document contained no indexable chunks."}

    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    course_code = meta.get("course_code", "GENERAL")
    doc_type = meta.get("doc_type", "LECTURE_NOTES")
    title = meta.get("title", filename)
    summary = meta.get("summary", "")
    aura_reward = 25

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO documents (id, uploader_id, course_code, title, doc_type, summary, aura_minted)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (doc_id, uploader_id, course_code, title, doc_type, summary, aura_reward))

        for chunk in chunks[:30]:
            vec = embed_text(chunk)
            cur.execute("""
            INSERT INTO document_chunks (document_id, uploader_id, chunk_text, embedding)
            VALUES (%s, %s, %s, %s::vector);
            """, (doc_id, uploader_id, chunk, vec_to_pg(vec)))
    conn.commit()
    conn.close()

    ledger_hash = commit_ledger(uploader_id, f"Seeded {doc_type}: {course_code} ({title})", float(aura_reward), 0.0)

    return {
        "accepted": True,
        "document_id": doc_id,
        "course_code": course_code,
        "doc_type": doc_type,
        "title": title,
        "summary": summary,
        "chunks_indexed": min(len(chunks), 30),
        "aura_minted": aura_reward,
        "ledger_hash": ledger_hash
    }

def query_campus_knowledge(query_text: str, querier_id: str, top_k: int = 4) -> Dict[str, Any]:
    """Searches pgvector for academic matches and pays +1 Aura Epistemic Royalties to uploaders."""
    query_vec = embed_text(query_text)
    vec_str = vec_to_pg(query_vec)

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
        SELECT dc.document_id, dc.uploader_id, dc.chunk_text,
               d.title, d.course_code, d.doc_type, u.name AS uploader_name,
               1 - (dc.embedding <=> %s::vector) AS similarity
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        JOIN users u ON dc.uploader_id = u.id
        ORDER BY dc.embedding <=> %s::vector
        LIMIT %s;
        """, (vec_str, vec_str, top_k))
        rows = cur.fetchall()
    conn.close()

    matches = [dict(r) for r in rows if float(r["similarity"]) >= 0.35]
    royalties_paid = []
    seen_docs = set()

    for m in matches:
        doc_id = m["document_id"]
        uploader_id = m["uploader_id"]
        if doc_id not in seen_docs:
            seen_docs.add(doc_id)
            if uploader_id != querier_id:
                conn = get_conn()
                with conn.cursor() as cur:
                    cur.execute("""
                    UPDATE documents SET queries_count = queries_count + 1, royalty_earned = royalty_earned + 1
                    WHERE id = %s;
                    """, (doc_id,))
                conn.commit()
                conn.close()
                commit_ledger(uploader_id, f"Epistemic Royalty: {m['course_code']} cited for peer", 1.0, 0.0)
                royalties_paid.append({"to": m["uploader_name"], "course": m["course_code"], "aura": 1})

    return {"matches": matches, "royalties_paid": royalties_paid}
