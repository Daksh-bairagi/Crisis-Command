Good instinct to pause and understand before writing. Let me explain the whole thing end to end.

---

## What CrisisCommand actually is

Imagine you're an SRE at a company. It's 2am. Your monitoring tool (like PagerDuty or Datadog) detects that the payments service is throwing 500 errors. Right now, manually, you have to:

1. Create an incident document
2. Block your calendar and set up a war room
3. Post an alert to your team's Chat
4. Search through past incidents to see if this happened before
5. Find the relevant runbook

That takes 10-15 minutes while production is burning.

**CrisisCommand does all of that in 30 seconds, autonomously.**

---

## The full flow, step by step

```
1. TRIGGER
   Your monitoring tool detects something wrong
   → sends a POST request to CrisisCommand's webhook
   → (in our demo, our simulator script fakes this)

2. WEBHOOK RECEIVES IT
   FastAPI webhook endpoint gets the alert
   → extracts: service name, severity, description
   → immediately returns "received" to the monitoring tool
   → kicks off processing in the background

3. ORCHESTRATOR TAKES OVER
   The main ADK agent wakes up
   → first classifies severity: is this P0, P1, or P2?
   → P0 = full response, P1 = partial, P2 = just log it
   → queries AlloyDB: "have we seen this before?"
   → AlloyDB vector search returns similar past incidents

4. AGENTS FAN OUT IN PARALLEL (the multi-agent part)
   Three agents activate simultaneously via A2A:

   Chat Agent          Docs Agent           Calendar Agent
       ↓                    ↓                     ↓
   Posts incident      Creates Google        Blocks on-call
   card to Chat        Doc with SRE          engineer calendar
   space with          template +            + creates Meet
   Acknowledge/        auto-fills            war room link
   Resolve buttons     timeline

5. ALLOYDB RAG RESULT INJECTED
   The similar past incident found in step 3
   → gets written into the incident Doc automatically
   → "Similar incident Aug 2024: DB connection pool exhaustion
      Resolution: restart connection pool manager
      Runbook: #4"
   → engineer opens the Doc and the answer is already there

6. ENGINEER SEES THE CHAT CARD
   Pops up in Google Chat space:
   "🚨 P0 Incident: Payments Service
    500 errors spike detected
    Doc: [link] | War Room: [Meet link]
    Similar past incident found ↓
    [Acknowledge] [Resolve]"

7. ENGINEER CLICKS ACKNOWLEDGE
   → webhook receives the button click
   → updates incident status in AlloyDB
   → updates the Chat card
   → timestamps the acknowledgment in the Doc timeline

8. ENGINEER CLICKS RESOLVE
   → incident marked resolved in AlloyDB
   → MTTR calculated: detected_at to resolved_at
   → postmortem section auto-filled in the Doc
   → Chat card updated: "✅ Resolved in 4m 32s"
```

---

## What each file does

```
webhook/main.py
└── The front door. Receives all incoming requests.
    Two types: monitoring alerts + Chat button clicks.

orchestrator/agent.py
└── The brain. ADK agent that coordinates everything.
    Decides severity, queries memory, fans out to sub-agents.

orchestrator/classifier.py
└── Single responsibility: P0/P1/P2 classification logic.
    Keeps the orchestrator clean.

agents/chat_agent/
└── Knows only how to talk to Google Chat.
    Post cards, update cards, send messages.

agents/docs_agent/
└── Knows only how to talk to Google Docs.
    Create doc from template, append timeline, share.

agents/calendar_agent/
└── Knows only how to talk to Google Calendar.
    Check availability, create event, add Meet link.

database/schema.sql
└── Three tables:
    incidents → structured data (id, service, severity, status, timestamps)
    incident_memory → vector embeddings of past incidents for RAG
    agent_traces → every agent action logged for trace UI

database/db.py
└── All database operations.
    store_incident, search_similar, log_trace, update_status

simulator/fire_incident.py
└── Fakes a PagerDuty webhook for the demo.
    Sends a POST to our webhook with realistic alert data.

ui/trace.html
└── Live view of agent reasoning.
    SSE stream from DB → browser shows each agent step in real time.

chat_app/cards.py
└── Builds the Google Chat card JSON.
    Incident card, update card, resolution card.
```

---

## What the judges see during demo

**T+0s** — you run the simulator script. Terminal shows "Alert sent."

**T+2s** — webhook terminal shows "🚨 P0 Alert received — payments service"

**T+3s** — orchestrator terminal shows agents activating in parallel

**T+5s** — trace UI in browser shows live agent steps scrolling in real time:
```
14:23:01 | orchestrator   | Classified as P0
14:23:01 | orchestrator   | Querying AlloyDB for similar incidents
14:23:02 | alloydb        | Found similar: Aug 2024 DB pool exhaustion
14:23:02 | chat_agent     | Posting incident card to space
14:23:02 | docs_agent     | Creating incident document
14:23:02 | calendar_agent | Blocking on-call calendar
14:23:03 | chat_agent     | Card posted successfully
14:23:03 | docs_agent     | Doc created: [link]
14:23:03 | calendar_agent | Meet link created: [link]
```

**T+8s** — Google Chat space shows the incident card with links

**T+10s** — you open the Doc, past incident context is already there

**T+15s** — you click Resolve on the card

**T+16s** — card updates to "✅ Resolved in 16s | MTTR logged"

That's the demo. 16 seconds from alert to resolution setup. Judges have never seen their calendar blocked and a Doc created simultaneously from a single chat alert.

---

Now you understand exactly what every line of code we write is doing and why.

Ready to write `webhook/main.py`?