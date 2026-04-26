from fastapi import FastAPI
from fastapi.responses import Response
import subprocess, tempfile, os, shutil

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/burn-captions")
def burn_captions():
    return {"ok": True}
