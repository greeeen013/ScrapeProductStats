#!/usr/bin/env python3
"""Scraper Manager - FastAPI backend"""

import asyncio
import json
import sys
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================
# Konfigurace scraperů
# ============================================================
SCRAPERS_DIR = Path(__file__).parent.parent

SCRAPERS = {
    "smicro": {
        "id": "smicro",
        "name": "SMicro.cz",
        "description": "Scraper pro smicro.cz – paměti, komponenty",
        "script": str(SCRAPERS_DIR / "smicroScrapePlayWright.py"),
        "output_csv": str(SCRAPERS_DIR / "smicro_products.csv"),
        "progress_file": str(SCRAPERS_DIR / "smicroScrapeLastProduct.json"),
        "inputs": [
            {
                "id": "workers",
                "label": "Počet workerů",
                "default": "2",
                "hint": "Doporučeno 2–3",
                "type": "number",
            },
            {
                "id": "category",
                "label": "Kategorie",
                "default": "vse",
                "hint": "'vse' = vše, číslo = konkrétní, 'X' = vlastní URL",
                "type": "text",
            },
            {
                "id": "category_url",
                "label": "Vlastní URL (jen pokud Kategorie = X)",
                "default": "",
                "hint": "Např. https://smicro.cz/pameti-ram",
                "type": "text",
                "condition": {"field": "category", "value": "x"},
            },
            {
                "id": "resume",
                "label": "Pokračovat od minula",
                "default": "ne",
                "hint": "'ano' nebo 'ne' – použije se jen pokud existuje progress soubor",
                "type": "select",
                "options": ["ano", "ne"],
            },
        ],
    },
    "it-planet": {
        "id": "it-planet",
        "name": "IT-Planet.cz",
        "description": "Scraper pro it-planet.cz – IT hardware",
        "script": str(SCRAPERS_DIR / "it-planetScrapePlayWright.py"),
        "output_csv": str(SCRAPERS_DIR / "it-planet_data.csv"),
        "progress_file": str(SCRAPERS_DIR / "it-planet_progress_v6.json"),
        "inputs": [
            {
                "id": "workers",
                "label": "Počet workerů",
                "default": "3",
                "hint": "Doporučeno 3–5",
                "type": "number",
            },
            {
                "id": "headless",
                "label": "Headless režim",
                "default": "ano",
                "hint": "'ano' = bez okna (server), 'ne' = s oknem",
                "type": "select",
                "options": ["ano", "ne"],
            },
            {
                "id": "section",
                "label": "Sekce",
                "default": "vse",
                "hint": "'vse' = vše, nebo číslo konkrétní sekce",
                "type": "text",
            },
        ],
    },
    "it-market": {
        "id": "it-market",
        "name": "IT-Market.com",
        "description": "Scraper pro it-market.com – IT vybavení",
        "script": str(SCRAPERS_DIR / "it-marketScrapePlayWright.py"),
        "output_csv": str(SCRAPERS_DIR / "it-market.csv"),
        "progress_file": str(SCRAPERS_DIR / "it-marketScrapeLastProduct.json"),
        "inputs": [
            {
                "id": "headless",
                "label": "Headless režim",
                "default": "ano",
                "hint": "'ano' = bez okna (server), 'ne' = s oknem",
                "type": "select",
                "options": ["ano", "ne"],
            },
            {
                "id": "workers",
                "label": "Počet workerů",
                "default": "5",
                "hint": "Doporučeno 3–5",
                "type": "number",
            },
            {
                "id": "max_pages",
                "label": "Max stránek na sekci",
                "default": "",
                "hint": "Prázdné = vše",
                "type": "text",
            },
            {
                "id": "section",
                "label": "Sekce",
                "default": "vse",
                "hint": "'vse' = vše, nebo číslo / název sekce",
                "type": "text",
            },
            {
                "id": "resume",
                "label": "Pokračovat od minula",
                "default": "ne",
                "hint": "'ano' nebo 'ne'",
                "type": "select",
                "options": ["ano", "ne"],
            },
        ],
    },
}

# Označení dostupnosti (script musí existovat)
for sid, cfg in SCRAPERS.items():
    cfg["available"] = Path(cfg["script"]).exists()

# ============================================================
# In-memory storage
# ============================================================
runs: Dict[str, dict] = {}

# ============================================================
# FastAPI app
# ============================================================
app = FastAPI(title="Scraper Manager")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================
# Models
# ============================================================
class StartRunRequest(BaseModel):
    scraper_id: str
    inputs: Dict[str, str]


# ============================================================
# Routes
# ============================================================
@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/scrapers")
async def get_scrapers():
    result = []
    for cfg in SCRAPERS.values():
        entry = {k: v for k, v in cfg.items()}
        # Přidat info o existenci progress souboru
        entry["has_progress"] = Path(cfg["progress_file"]).exists()
        # Přidat info o existenci výstupního CSV
        entry["has_output"] = Path(cfg["output_csv"]).exists()
        result.append(entry)
    return result


@app.get("/api/runs")
async def get_runs():
    result = []
    for r in reversed(list(runs.values())):
        result.append(_run_public(r))
    return result


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    if run_id not in runs:
        raise HTTPException(404, "Run nenalezen")
    run = runs[run_id]
    data = _run_public(run)
    data["logs"] = list(run["_logs"])
    return data


@app.post("/api/runs")
async def start_run(req: StartRunRequest):
    if req.scraper_id not in SCRAPERS:
        raise HTTPException(400, "Neznámý scraper")
    scraper = SCRAPERS[req.scraper_id]
    if not scraper.get("available"):
        raise HTTPException(400, "Script scraperu nenalezen")

    run_id = str(uuid.uuid4())[:8]
    run = {
        "id": run_id,
        "scraper_id": req.scraper_id,
        "scraper_name": scraper["name"],
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "exit_code": None,
        "inputs": req.inputs,
        "_logs": deque(maxlen=500),
        "_subscribers": set(),
        "_process": None,
    }
    runs[run_id] = run

    stdin_lines = _build_stdin(scraper, req.inputs)
    asyncio.create_task(_run_scraper(run_id, scraper["script"], stdin_lines))

    return {"run_id": run_id, "status": "started"}


@app.delete("/api/runs/{run_id}")
async def stop_run(run_id: str):
    if run_id not in runs:
        raise HTTPException(404, "Run nenalezen")
    run = runs[run_id]
    proc = run.get("_process")
    if proc and run["status"] == "running":
        proc.terminate()
        run["status"] = "stopped"
        run["finished_at"] = datetime.now().isoformat()
    return {"status": "ok"}


@app.get("/api/runs/{run_id}/download")
async def download_result(run_id: str):
    if run_id not in runs:
        raise HTTPException(404, "Run nenalezen")
    run = runs[run_id]
    scraper = SCRAPERS.get(run["scraper_id"])
    if not scraper:
        raise HTTPException(400)
    csv_path = Path(scraper["output_csv"])
    if not csv_path.exists():
        raise HTTPException(404, "Výstupní soubor nenalezen – scraper ještě nedoběhl nebo nic nestáhl")
    return FileResponse(str(csv_path), filename=csv_path.name, media_type="text/csv")


@app.websocket("/ws/runs/{run_id}")
async def ws_logs(websocket: WebSocket, run_id: str):
    await websocket.accept()
    if run_id not in runs:
        await websocket.close(code=1008)
        return
    run = runs[run_id]

    # Odeslat existující logy
    for line in list(run["_logs"]):
        try:
            await websocket.send_text(line)
        except Exception:
            return

    if run["status"] != "running":
        await websocket.close()
        return

    run["_subscribers"].add(websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=20)
            except asyncio.TimeoutError:
                if run["status"] != "running":
                    break
                # keepalive ping
                try:
                    await websocket.send_text("\x00")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        run["_subscribers"].discard(websocket)


# ============================================================
# Helpers
# ============================================================
def _run_public(run: dict) -> dict:
    return {
        "id": run["id"],
        "scraper_id": run["scraper_id"],
        "scraper_name": run["scraper_name"],
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "exit_code": run["exit_code"],
        "inputs": run["inputs"],
        "log_lines": len(run["_logs"]),
    }


def _build_stdin(scraper: dict, inputs: dict) -> List[str]:
    lines = []
    for inp in scraper["inputs"]:
        fid = inp["id"]
        # Přeskočit category_url pokud kategorie není X
        if fid == "category_url":
            if inputs.get("category", "").strip().lower() != "x":
                continue
        val = inputs.get(fid, inp.get("default", ""))
        lines.append(val)
    return lines


async def _run_scraper(run_id: str, script: str, stdin_lines: List[str]):
    run = runs[run_id]
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(script).parent),
        )
        run["_process"] = proc

        stdin_data = "\n".join(stdin_lines) + "\n"
        proc.stdin.write(stdin_data.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async def _broadcast(text: str):
            dead: Set = set()
            for ws in run["_subscribers"]:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.add(ws)
            run["_subscribers"] -= dead

        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").rstrip()
            run["_logs"].append(text)
            await _broadcast(text)

        await proc.wait()
        if run["status"] == "running":
            run["status"] = "completed" if proc.returncode == 0 else "failed"
        run["exit_code"] = proc.returncode

    except Exception as e:
        run["_logs"].append(f"[MANAGER ERROR] {e}")
        if run["status"] == "running":
            run["status"] = "failed"
    finally:
        run["finished_at"] = datetime.now().isoformat()
        run["_process"] = None


# ============================================================
# Spuštění
# ============================================================
if __name__ == "__main__":
    import uvicorn

    print("=== Scraper Manager ===")
    print("Otevři prohlížeč na: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
