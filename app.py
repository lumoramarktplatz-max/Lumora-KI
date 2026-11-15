"""Lumora-KI: ein allgemeiner KI-Assistent mit Streaming-Chat."""
import json
import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

MODEL = os.getenv("LUMORA_MODEL", "claude-sonnet-5-5")
SYSTEM_PROMPT = (
    "Du bist Lumora, eine freundliche und hilfreiche KI. "
    "Du beantwortest Fragen zu allen Themen klar, ehrlich und verständlich. "
    "Antworte in der Sprache des Nutzers (standardmäßig Deutsch). "
    "Wenn du etwas nicht weißt, sag es offen."
)

client = AsyncAnthropic()  # liest ANTHROPIC_API_KEY aus der Umgebung
app = FastAPI(title="Lumora-KI")


class Message(BaseModel):
    role: str  # "user" oder "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]


@app.post("/api/chat")
async def chat(req: ChatRequest):
    async def stream():
        try:
            async with client.messages.stream(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[m.model_dump() for m in req.messages],
            ) as s:
                async for text in s.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as e:  # Fehler an die UI weiterreichen
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
