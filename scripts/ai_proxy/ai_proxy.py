"""
Lightweight replacement for AppFlowy AI container.

Translates AppFlowy Cloud AI requests to OpenAI-compatible API calls.
"""

import json
import os
import logging
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
DEFAULT_MODEL = os.environ.get("DEFAULT_AI_MODEL", "mistral-small-latest")
DEFAULT_COMPLETION_MODEL = os.environ.get("DEFAULT_AI_COMPLETION_MODEL", "mistral-small-latest")
PORT = int(os.environ.get("AI_SERVER_PORT", "5001"))

COMPLETION_PROMPTS = {
    1: "Improve the writing of the following text. Only output the improved text, no explanations.",
    2: "Fix spelling and grammar in the following text. Only output the corrected text.",
    3: "Make the following text shorter while preserving key information.",
    4: "Make the following text longer by adding relevant details.",
    5: "Continue writing the following text naturally.",
    6: "Explain the following text in simple terms.",
    7: "",
    8: "",
}


def ai_response(data=None, message=""):
    resp = {"message": message}
    if data is not None:
        resp["data"] = data
    return resp


def resolve_model(request: Request) -> str:
    model = request.headers.get("ai-model", "")
    if not model or model.lower() == "auto":
        return DEFAULT_MODEL
    return model


async def stream_openai_completion(messages: list[dict], model: str) -> AsyncGenerator[str, None]:
    if not messages or not any(m.get("content", "").strip() for m in messages):
        yield "I'm sorry, I couldn't process your request. Please try again."
        return

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{OPENAI_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "max_tokens": 4096,
                },
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    logger.error(f"Upstream {response.status_code}: {body.decode()[:200]}")
                    yield f"AI service error ({response.status_code}). Please try again."
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
    except httpx.TimeoutException:
        yield "AI request timed out. Please try again."
    except Exception as e:
        logger.error(f"Stream error: {e}")
        yield f"AI service error: {str(e)[:100]}"


async def non_stream_openai_completion(messages: list[dict], model: str) -> str:
    if not messages or not any(m.get("content", "").strip() for m in messages):
        return ""

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{OPENAI_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def build_messages_from_completion(body: dict) -> list[dict]:
    text = body.get("text", "")
    completion_type = body.get("completion_type", 7)
    metadata = body.get("metadata") or {}

    system_prompt = COMPLETION_PROMPTS.get(completion_type, "")
    custom_prompt = metadata.get("custom_prompt", "")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    elif completion_type == 8 and custom_prompt:
        messages.append({"role": "system", "content": custom_prompt})

    messages.append({"role": "user", "content": text})
    return messages


def extract_chat_content(body: dict) -> str:
    data = body.get("data", {})
    if isinstance(data, dict):
        content = data.get("content", "")
        if isinstance(content, list):
            return " ".join(str(x) for x in content)
        return str(content)
    return ""


# --- Health ---

@app.get("/health")
async def health():
    return {"self_hosted": True, "status": "healthy"}


# --- Model list ---

@app.get("/model/list")
async def model_list():
    return ai_response(data={"models": [{"name": DEFAULT_MODEL}]})


# --- Search ---

@app.get("/search")
async def search():
    return ai_response(data=[])


# --- Completion stream ---

@app.post("/completion/stream")
async def completion_stream(request: Request):
    body = await request.json()
    model = resolve_model(request)
    messages = build_messages_from_completion(body)

    async def generate():
        async for chunk in stream_openai_completion(messages, model):
            yield json.dumps({"1": chunk}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/v2/completion/stream")
async def completion_v2_stream(request: Request):
    body = await request.json()
    model = resolve_model(request)
    messages = build_messages_from_completion(body)

    async def generate():
        async for chunk in stream_openai_completion(messages, model):
            yield json.dumps({"1": chunk}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# --- Summarize row ---

@app.post("/summarize_row")
async def summarize_row(request: Request):
    body = await request.json()
    model = resolve_model(request)
    content = extract_chat_content(body)

    messages = [
        {"role": "system", "content": "Summarize the following content concisely in 1-2 sentences."},
        {"role": "user", "content": content},
    ]
    result = await non_stream_openai_completion(messages, model)
    return ai_response(data={"text": result})


# --- Translate row ---

@app.post("/translate_row")
async def translate_row(request: Request):
    body = await request.json()
    model = resolve_model(request)
    data = body.get("data", {})
    content = data.get("content", "")
    language = data.get("language", "English")

    messages = [
        {"role": "system", "content": f"Translate the following text to {language}. Only output the translation."},
        {"role": "user", "content": content},
    ]
    result = await non_stream_openai_completion(messages, model)
    return ai_response(data={"items": [{"content": result}]})


# --- Calculate similarity ---

@app.post("/calculate_similarity")
async def calculate_similarity():
    return ai_response(data={"similarity": 0.0})


# Cloud client calls {url}/similarity (not /calculate_similarity)
@app.post("/similarity")
async def similarity():
    return ai_response(data={"similarity": 0.0})


# --- Chat context ---

@app.post("/chat/context/text")
async def chat_context_text():
    return ai_response()


# --- Chat message (non-streaming) ---

@app.post("/chat/message")
async def chat_message(request: Request):
    body = await request.json()
    model = resolve_model(request)
    content = extract_chat_content(body)

    if not content.strip():
        return ai_response(data={"content": "Please provide a question.", "message_id": 0})

    messages = [{"role": "user", "content": content}]
    result = await non_stream_openai_completion(messages, model)
    return ai_response(data={"content": result, "message_id": 0})


# --- Chat message stream (v1) ---

@app.post("/chat/message/stream")
async def chat_message_stream(request: Request):
    body = await request.json()
    model = resolve_model(request)
    content = extract_chat_content(body)

    messages = [{"role": "user", "content": content}]

    async def generate():
        async for chunk in stream_openai_completion(messages, model):
            yield json.dumps({"1": chunk}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# --- Chat message stream (v2) ---

@app.post("/v2/chat/message/stream")
async def chat_message_v2_stream(request: Request):
    body = await request.json()
    model = resolve_model(request)
    content = extract_chat_content(body)

    messages = [{"role": "user", "content": content}]

    async def generate():
        async for chunk in stream_openai_completion(messages, model):
            yield json.dumps({"1": chunk}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# --- Chat related questions ---

@app.get("/chat/{chat_id}/{message_id}/related_question")
async def related_question(chat_id: str, message_id: str):
    return ai_response(data={"items": []})


# --- Local config ---

@app.get("/local/config")
async def local_config():
    return ai_response(data={"ai_model": DEFAULT_MODEL, "enabled": True})


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting AI proxy on 0.0.0.0:{PORT}")
    logger.info(f"Using OpenAI-compatible API at: {OPENAI_API_BASE}")
    logger.info(f"Default model: {DEFAULT_MODEL}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
