import os
from datetime import datetime, timezone
from typing import Optional, List, Literal, Dict, Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import create_document, get_documents, db

app = FastAPI(title="Personal Assistant Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Models
# -----------------------------
class ParseRequest(BaseModel):
    text: str


class Intent(BaseModel):
    type: Literal[
        "open_app",
        "toggle_setting",
        "adjust_setting",
        "unknown",
    ] = "unknown"
    target: Optional[str] = None
    action: Optional[str] = None  # open | on | off | increase | decrease | set
    value: Optional[Any] = None
    raw_text: Optional[str] = None


class Interaction(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    intent: Optional[Intent] = None


class EmotionState(BaseModel):
    mood: Literal[
        "happy",
        "neutral",
        "sad",
        "stressed",
        "calm",
        "excited",
        "tired",
    ] = "neutral"
    arousal: int = Field(5, ge=1, le=10, description="Energy level 1-10")
    notes: Optional[str] = None


class DeviceAction(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["open_app", "toggle", "adjust"]
    target: str
    action: Optional[str] = None
    value: Optional[Any] = None
    status: Literal["pending", "reserved", "completed", "failed"] = "pending"
    device_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


# -----------------------------
# Helpers
# -----------------------------
COMMON_APPS = {
    "whatsapp": ["whatsapp", "whats app"],
    "instagram": ["instagram", "insta"],
    "youtube": ["youtube", "yt"],
    "twitter": ["twitter", "x"],
    "facebook": ["facebook", "fb"],
    "camera": ["camera"],
    "settings": ["settings"],
    "chrome": ["chrome", "browser"],
    "gmail": ["gmail", "mail"],
}

SETTINGS = {
    "wifi": ["wi-fi", "wifi"],
    "bluetooth": ["bluetooth"],
    "data": ["mobile data", "data", "cellular"],
    "flashlight": ["flashlight", "torch"],
    "airplane": ["airplane", "flight mode"],
    "dnd": ["do not disturb", "dnd"],
    "location": ["location", "gps"],
    "hotspot": ["hotspot", "tethering"],
}

ADJUSTABLES = {
    "volume": ["volume", "sound"],
    "brightness": ["brightness", "display"],
}


def normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


def parse_intent(text: str) -> Intent:
    t = normalize(text)
    intent = Intent(type="unknown", raw_text=text)

    # Open app
    if t.startswith("open ") or t.startswith("launch "):
        app_name = t.split(" ", 1)[1]
        # map to known app keys if possible
        for key, aliases in COMMON_APPS.items():
            if app_name in aliases or key in app_name:
                intent.type = "open_app"
                intent.target = key
                intent.action = "open"
                return intent
        # default to given name
        intent.type = "open_app"
        intent.target = app_name
        intent.action = "open"
        return intent

    # Toggle settings on/off
    if any(w in t for w in ["turn on", "enable", "switch on"]).__bool__():
        for key, aliases in SETTINGS.items():
            if any(a in t for a in aliases):
                intent.type = "toggle_setting"
                intent.target = key
                intent.action = "on"
                return intent

    if any(w in t for w in ["turn off", "disable", "switch off"]).__bool__():
        for key, aliases in SETTINGS.items():
            if any(a in t for a in aliases):
                intent.type = "toggle_setting"
                intent.target = key
                intent.action = "off"
                return intent

    # Adjust settings up/down/set
    if any(w in t for w in ["increase", "turn up", "raise", "decrease", "turn down", "lower", "set "]):
        action = None
        if any(w in t for w in ["increase", "turn up", "raise"]):
            action = "increase"
        elif any(w in t for w in ["decrease", "turn down", "lower"]):
            action = "decrease"
        elif t.startswith("set "):
            action = "set"
        for key, aliases in ADJUSTABLES.items():
            if any(a in t for a in aliases):
                value = None
                # extract percentage if present
                import re
                m = re.search(r"(\d+)%", t)
                if m:
                    value = int(m.group(1))
                intent.type = "adjust_setting"
                intent.target = key
                intent.action = action
                intent.value = value
                return intent

    # Fallback unknown
    return intent


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def read_root():
    return {"message": "Assistant backend running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, "name") else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


@app.post("/api/parse", response_model=Intent)
def api_parse(req: ParseRequest):
    intent = parse_intent(req.text)
    return intent


@app.post("/api/interactions")
def create_interaction(item: Interaction):
    doc = item.model_dump()
    _id = create_document("interaction", doc)
    # If intent is actionable, enqueue a device action
    if item.intent and item.intent.type in {"open_app", "toggle_setting", "adjust_setting"}:
        if item.intent.type == "open_app":
            action = DeviceAction(kind="open_app", target=item.intent.target or "", action="open")
        elif item.intent.type == "toggle_setting":
            action = DeviceAction(kind="toggle", target=item.intent.target or "", action=item.intent.action)
        else:
            action = DeviceAction(kind="adjust", target=item.intent.target or "", action=item.intent.action, value=item.intent.value)
        create_document("deviceaction", action.model_dump())
    return {"id": _id}


@app.get("/api/interactions")
def list_interactions(limit: int = Query(50, ge=1, le=200)):
    docs = get_documents("interaction", {}, limit)
    return [{**d, "_id": str(d.get("_id"))} for d in docs]


@app.post("/api/emotions")
def set_emotion(state: EmotionState):
    _id = create_document("emotionstate", state.model_dump())
    return {"id": _id}


@app.get("/api/emotions/latest")
def get_latest_emotion():
    docs = get_documents("emotionstate", {}, 1)
    if not docs:
        return EmotionState().model_dump()
    doc = docs[-1]
    return {k: v for k, v in doc.items() if k in {"mood", "arousal", "notes"}}


# Device companion API
@app.get("/api/actions/next", response_model=Optional[DeviceAction])
def get_next_action(device_id: Optional[str] = None):
    # naive: just return the first pending
    pending = db["deviceaction"].find_one({"status": "pending"}) if db else None
    if not pending:
        return None
    db["deviceaction"].update_one({"_id": pending["_id"]}, {"$set": {"status": "reserved", "device_id": device_id, "updated_at": datetime.now(timezone.utc)}})
    pending["id"] = str(pending.get("id") or pending.get("_id"))
    pending["_id"] = str(pending["_id"])  # make JSON safe
    return DeviceAction(
        id=pending.get("id"),
        kind=pending.get("kind"),
        target=pending.get("target"),
        action=pending.get("action"),
        value=pending.get("value"),
        status="reserved",
        device_id=device_id,
    )


class CompleteActionRequest(BaseModel):
    result: Optional[Dict[str, Any]] = None
    status: Literal["completed", "failed"] = "completed"


@app.post("/api/actions/{action_id}/complete")
def complete_action(action_id: str, payload: CompleteActionRequest):
    if db is None:
        raise HTTPException(500, "Database not configured")
    res = db["deviceaction"].update_one(
        {"id": action_id},
        {"$set": {"status": payload.status, "result": payload.result, "updated_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Action not found")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
