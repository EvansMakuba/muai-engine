from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="MuAI Epistemic Engine", version="0.1.0")

@app.get("/health")
async def health_check():
    return JSONResponse({"status": "operational", "node": "Moi-University-PoC", "port": 8082})

@app.get("/")
async def root():
    return {"message": "MuAI Engine Online. Pipeline verified."}
