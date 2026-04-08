from fastapi import FastAPI, Request,HTTPException,BackgroundTasks
from fastapi.responses import JSONResponse
import hmac
import hashlib
import json
import sys
import os
import asyncio

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from logger import get_logger
from orchestrator.agent import process_incident_alert
from database.db import update_incident_status

log= get_logger("webhook")
app= FastAPI(title="CrisisCommand Webhook", description="Webhook for CrisisCommand to receive alerts and trigger actions.", version="1.0")


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
        return await handle_chat_message(body)

    elif event_type == "CARD_CLICKED":
        return await handle_card_click(body, background_tasks)

    return JSONResponse({"status": "unhandled"})

async def handle_chat_message(body:dict)->JSONResponse:
    """Handles text messages sent to the bot"""
    text= body.get("message",{}).get("text","").strip().lower()
    sender= body.get("message",{}).get("sender",{}).get("displayName","Unknown")
    log.info(f"Received message from {sender}: {text}")

    if "help" in text:
        return JSONResponse({"text": (
            "📖 *CrisisCommand Commands*\n"
            "`status` — system health\n"
            "`incidents` — list active incidents\n"
            "Incidents are triggered automatically via monitoring webhooks."
        )})
    elif "status" in text:
        return JSONResponse({
            "text": "✅ CrisisCommand operational. Monitoring active."
        })
    else:
        return JSONResponse({
            "text": "❓ Sorry, I didn't understand that. Type `help` for commands."
        })
    

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
    """
    {"key": "incident_id", "value": "INC123"} 
    to 
    "incident_id": "INC123"
    """
    incident_id= parameter.get("incident_id","unknown")
    actor= body.get("user",{}).get("displayName","Unknown")

    log.info(f"Button clicked: {action_name} on incident {incident_id} by {actor}") 

    if action_name== "acknowledge":
        # Update AlloyDB record to acknowledged
        asyncio.create_task(update_incident_status(incident_id, "acknowledged", actor))
        log.info(f"Incident {incident_id} acknowledged by {actor}")
        return JSONResponse(
            {"text":f"Incidnent {incident_id} acknowledged. Thank you, {actor}!"}
        )
    elif action_name== "resolve":
        # Update AlloyDB record to resolved
        asyncio.create_task(update_incident_status(incident_id, "resolved", actor))
        log.info(f"Incident {incident_id} resolved by {actor}")
        return JSONResponse(
            {"text":f"Incidnent {incident_id} resolved. Great work, {actor}!"}
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
    
    # Call orchestrator to handle the incident
    result = await process_incident_alert(alert)
    
    if result.get("success"):
        log.info(f"✅ Incident orchestrated: {result.get('incident_id')} ({result.get('severity')})")
    else:
        log.error(f"❌ Orchestration failed: {result.get('error')}")

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








"""
verify_chat_request()
└── Checks if request came from Google Chat (Bearer token check)

verify_simulator_request()  
└── Checks if request came from our simulator (secret key check)

webhook_handler()           ← THE FRONT DOOR
└── Receives ALL incoming POST requests
    Reads event type → routes to correct handler
    Monitoring alerts → background task (don't wait)
    Chat events → immediate response

handle_chat_event()
└── Sub-router for Chat specifically
    ADDED_TO_SPACE → greet the space
    MESSAGE → handle_chat_message()
    CARD_CLICKED → handle_card_click()

handle_chat_message()
└── Handles text commands typed to the bot
    "help", "status" → returns text response

handle_card_click()
└── Handles button clicks on incident cards
    "acknowledge" / "resolve" → updates incident
    Carries incident_id so we know which incident

handle_monitoring_alert()
└── Background task — processes the actual alert
    Runs AFTER webhook already returned 200
    Day 2: this calls the orchestrator

health()
└── GET /health — just returns "ok"
    Cloud Run uses this to check service is alive

------------------------------------------------------------------
------------------------------------------------------------------
Simulator POST → webhook_handler → handle_monitoring_alert (background)
                                          ↓
                               orchestrator goes here Day 2

Chat button click → webhook_handler → handle_card_click → acknowledge/resolve


"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
