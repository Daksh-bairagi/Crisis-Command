from fastapi import FastAPI, Request,HTTPException,BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import hmac
import hashlib
import json
import sys
import os
import asyncio

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import get_logger

# Optional database imports - app works without database
try:
    from orchestrator.agent import process_incident_alert
    from orchestrator.post_mortem import generate_post_mortem
    from database.db import (
        update_incident_status,
        update_incident_resolution,
        store_incident,
        log_trace,
        get_recent_incidents,
        get_incident,
        store_incident_memory,
        search_similar_incidents,
    )
    DB_AVAILABLE = True
except Exception as e:
    log_msg = f"Database not available: {str(e)}"
    process_incident_alert = None
    generate_post_mortem = None
    update_incident_status = None
    update_incident_resolution = None
    store_incident = None
    log_trace = None
    get_recent_incidents = None
    get_incident = None
    store_incident_memory = None
    search_similar_incidents = None
    DB_AVAILABLE = False

log= get_logger("webhook")
if not DB_AVAILABLE:
    log.warning("Running in database-less mode. Core features disabled.")
app= FastAPI(title="CrisisCommand Webhook", description="Webhook for CrisisCommand to receive alerts and trigger actions.", version="1.0")

# Serve static UI files
UI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui")
app.mount("/ui", StaticFiles(directory=UI_PATH), name="ui")


def verify_chat_request(headers:dict)->bool:
    """
     Verify request came from Google Chat, not a random internet actor.
    
    Production concept: Google Chat signs requests with a Bearer JWT token.
    We check the header exists. In full production you'd verify the JWT
    signature against Google's public keys.
    
    This is called "defence in depth" — even if someone finds your ngrok URL,
    they can't trigger fake incidents without the auth header.
    """
    auth_headers= headers.get("authorization","")

    if not auth_headers.startswith("Bearer "):
        log.warning("Missing or invalid authorization header.")
        return False
    return True

def verify_simulator_request(header:dict)->bool:
    """
     Our simulator sends a secret key in headers.
    This prevents anyone else from triggering fake incidents.
    Simple but effective for a hackathon demo
    """
    secret= header.get("x-simulator-secret","") or header.get("x-simulator_secret","")

    return secret == os.getenv("SIMULATOR_SECRET","crisis-dev-secret")

@app.post("/webhook")
async def webhook_handler(request:Request,background_tasks:BackgroundTasks):
    """
    Single entry point for all events 
    Two SOurces are there 
    1. Google Chat - for real alerts from chat
    2. Simulator - for testing and demo purposes

    we disntinguish them by looking at event types
    """

    body= await request.json()
    headers= dict(request.headers)
    event_type= body.get("type","")

    log.info(f"Received event: {event_type}")

    if event_type in ("MESSAGE", "CARD_CLICKED","ADDED_TO_SPACE"):
        if not verify_chat_request(headers):
            raise HTTPException(status_code=401, detail="Unauthorized")
        # Process Google Chat event in background
        return await handle_chat_event(event_type, body, background_tasks)
     
    elif event_type== "MONITORING_ALERT":
        if not verify_simulator_request(headers):
            raise HTTPException(status_code=401, detail="Unauthorized")
        # Process monitoring alert in background
        background_tasks.add_task(handle_monitoring_alert, body)
        return JSONResponse(content={"status": "Alert received","message": "Processing alert in background"})
    else:
        log.warning(f"Unknown event type: {event_type}")
        return JSONResponse(content={"status": "ignored"})

@app.post("/webhook/monitoring-alert")
async def monitoring_alert_handler(request:Request, background_tasks:BackgroundTasks):
    """Direct endpoint for monitoring alerts (used by dashboard UI)"""
    body = await request.json()
    headers = dict(request.headers)
    
    if not verify_simulator_request(headers):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Process monitoring alert in background
    background_tasks.add_task(handle_monitoring_alert, body)
    return JSONResponse(content={"status": "Alert received", "message": "Processing alert in background"})
    
async def handle_chat_event(
    event_type: str,
    body: dict,
    background_tasks: BackgroundTasks
) -> JSONResponse:
    """Routes Chat events to specific handlers"""
    
    if event_type == "ADDED_TO_SPACE":
        # Bot was added to a space — introduce itself
        space_name = body.get("space", {}).get("displayName", "this space")
        log.info("Bot added to space: %s", space_name)
        return JSONResponse({
            "text": "👋 CrisisCommand is online. I'll automatically respond to incidents. Type `help` for commands."
        })

    elif event_type == "MESSAGE":
        return await handle_chat_message(body, background_tasks)

    elif event_type == "CARD_CLICKED":
        return await handle_card_click(body, background_tasks)

    return JSONResponse({"status": "unhandled"})

async def handle_chat_message(body: dict, background_tasks: BackgroundTasks) -> JSONResponse:
    """Handles text messages sent to the bot"""
    raw_text = body.get("message", {}).get("text", "").strip()
    text_lower = raw_text.lower()
    sender = body.get("message", {}).get("sender", {}).get("displayName", "Unknown")
    log.info("Received message from %s: %s", sender, raw_text)

    # resolution INC-XXXXXXXX <what was done>
    if text_lower.startswith("resolution "):
        parts = raw_text.split(" ", 2)
        if len(parts) < 3:
            return JSONResponse({"text": "Usage: `resolution INC-XXXXXXXX <what you did to fix it>`"})
        incident_id = parts[1].upper()
        notes = parts[2].strip()
        if not notes:
            return JSONResponse({"text": "Please include what you did to resolve the incident."})
        if DB_AVAILABLE and update_incident_resolution is not None:
            saved = await update_incident_resolution(incident_id, notes)
            if saved:
                background_tasks.add_task(_reembed_incident_memory, incident_id, notes)
                return JSONResponse({
                    "text": f"✅ Resolution notes saved for {incident_id}. Incident memory updated for future reference."
                })
        return JSONResponse({"text": f"❌ Could not save resolution notes for {incident_id}."})

    if "help" in text_lower:
        return JSONResponse({"text": (
            "📖 *CrisisCommand Commands*\n"
            "`status` — system health\n"
            "`incidents` — list active incidents\n"
            "`resolution INC-XXXXXXXX <what you did>` — record how you resolved an incident\n"
            "Incidents are triggered automatically via monitoring webhooks."
        )})
    elif "status" in text_lower:
        return JSONResponse({"text": "✅ CrisisCommand operational. Monitoring active."})
    else:
        return JSONResponse({"text": "❓ Sorry, I didn't understand that. Type `help` for commands."})
    

async def handle_card_click(body:dict, background_tasks: BackgroundTasks)->JSONResponse:
    """
    Handle button clicks on incident cards.
    
    Production concept: every button click carries the incident_id
    as a parameter. This is how we know WHICH incident to update
    when multiple incidents are active simultaneously.
    """
    action= body.get("action",{})
    action_name= action.get("actionMethodName","")
    parameter= {
        p["key"]:p["value"]
        for p in action.get("parameters",[])
    }
    incident_id= parameter.get("incident_id","unknown")
    actor= body.get("user",{}).get("displayName","Unknown")

    log.info(f"Button clicked: {action_name} on incident {incident_id} by {actor}") 

    if action_name == "acknowledge":
        background_tasks.add_task(update_incident_status, incident_id, "acknowledged")
        log.info(f"Incident {incident_id} acknowledged by {actor}")
        return JSONResponse(
            {"text": f"Incident {incident_id} acknowledged. Thank you, {actor}!"}
        )
    elif action_name == "resolve":
        background_tasks.add_task(update_incident_status, incident_id, "resolved")
        if generate_post_mortem is not None:
            background_tasks.add_task(generate_post_mortem, incident_id)
        log.info(f"Incident {incident_id} resolved by {actor}")
        return JSONResponse(
            {"text": f"Incident {incident_id} resolved. Great work, {actor}! Generating post-mortem..."}
        )
    else:
        log.warning(f"Unknown action: {action_name}")
        return JSONResponse({"text": "Unknown action."})
    
async def handle_monitoring_alert(body:dict):
    """
    Background task for processing monitoring alerts.
    
    This runs AFTER the webhook already returned 200 to the simulator.
    So this can take as long as needed without timeout issues.
    
    Calls the orchestrator agent to coordinate incident response.
    """
    alert = body.get("alert", {})
    service = alert.get("service", "unknown")
    description = alert.get("description", "No description provided")
    
    log.info("🚨 Processing alert | Service: %s", service)
    log.info("Description: %s", description)
    
    # Call orchestrator to handle the incident (if available)
    if process_incident_alert:
        result = await process_incident_alert(alert)
        
        if result.get("success"):
            log.info(f"✅ Incident orchestrated: {result.get('incident_id')} ({result.get('severity')})")
        else:
            log.error(f"❌ Orchestration failed: {result.get('error')}")
    else:
        log.warning("Orchestrator not available - incident logged but not processed")

@app.get("/")
async def root():
    """Serve dashboard.html at root"""
    dashboard_path = os.path.join(UI_PATH, "dashboard.html")
    if os.path.exists(dashboard_path):
        return FileResponse(dashboard_path, media_type="text/html")
    return {"message": "Dashboard not found"}

@app.get("/health")
async def health():
    """simple health check
    Health check — Cloud Run pings this to verify the service is alive.
    Always implement this. Zero effort, expected by every deployment platform.

    """
    return {
        "status":"ok",
        "service":"CrisisCommand",
        "version":"0.1.0"
    }

@app.get("/incidents")
async def get_incidents():
    """Get list of recent incidents for the dashboard."""
    if get_recent_incidents is not None:
        try:
            incidents = await get_recent_incidents(limit=20)
            return {"incidents": incidents}
        except Exception as e:
            log.warning("Could not fetch incidents from DB: %s", e)
    return {"incidents": []}


@app.post("/incidents/{incident_id}/resolution")
async def add_resolution(incident_id: str, request: Request, background_tasks: BackgroundTasks):
    """
    Record what the engineer actually did to resolve the incident.

    Body: {"notes": "rolled back deploy-447, increased DB pool from 10 to 50", "actor": "Jane"}

    This does two things:
    1. Stores resolution_notes on the incident record.
    2. Re-embeds the incident memory so future RAG queries surface
       the resolution alongside the problem description.
    """
    body = await request.json()
    notes = body.get("notes", "").strip()
    actor = body.get("actor", "System")

    if not notes:
        raise HTTPException(status_code=400, detail="'notes' field is required")
    if not DB_AVAILABLE or update_incident_resolution is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    saved = await update_incident_resolution(incident_id, notes)
    if not saved:
        raise HTTPException(status_code=500, detail="Failed to save resolution notes")

    log.info("Resolution notes added to %s by %s", incident_id, actor)
    background_tasks.add_task(_reembed_incident_memory, incident_id, notes)

    return {"status": "ok", "incident_id": incident_id, "message": "Resolution notes saved. Memory will be updated."}


async def _reembed_incident_memory(incident_id: str, resolution_notes: str):
    """
    Re-embed the incident memory entry with the resolution included.
    Future RAG searches will return both the problem AND how it was resolved.
    """
    try:
        import os
        import google.generativeai as genai

        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key or store_incident_memory is None or get_incident is None:
            return

        incident = await get_incident(incident_id)
        if not incident:
            log.warning("_reembed_incident_memory: incident %s not found", incident_id)
            return

        mttr = incident.get("mttr_seconds") or 0
        mttr_label = f"{mttr // 60}m {mttr % 60}s" if mttr else "unknown"

        # Build enriched content that includes the actual resolution
        content = (
            f"[{incident['severity']}] {incident['service']}: {incident['description']} "
            f"— {incident['likely_cause']}. "
            f"RESOLUTION APPLIED: {resolution_notes}. "
            f"MTTR: {mttr_label}."
        )

        genai.configure(api_key=api_key)
        response = await asyncio.wait_for(
            asyncio.to_thread(
                genai.embed_content,
                model="models/gemini-embedding-001",
                content=content,
                task_type="RETRIEVAL_DOCUMENT",
            ),
            timeout=10.0,
        )

        await store_incident_memory(
            incident_id=incident_id,
            content=content,
            embedding=response["embedding"],
            source="resolution",
        )
        log.info("Re-embedded incident memory for %s with resolution context", incident_id)

    except Exception as e:
        log.error("Failed to re-embed memory for %s: %s", incident_id, e)








if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
