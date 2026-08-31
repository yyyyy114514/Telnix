"""AI analysis API: analyze flows + multi-turn chat + record management + SSE streaming."""

import asyncio
import json
import re

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from ..ai import deepseek
from . import err, ok

router = APIRouter()


class AnalyzeRequest(BaseModel):
    flow_ids: list[int] = []


class ChatRequest(BaseModel):
    chat_id: int
    message: str
    flow_ids: list[int] = []


class UpdateTitleRequest(BaseModel):
    title: str


class UpdateServiceRequest(BaseModel):
    service: str


class UpdateModelRequest(BaseModel):
    service: str
    model: str


def _build_flow_context(flow_ids: list[int]) -> str:
    """Build flow context text from flow_ids."""
    by_id = db.get_flows_by_ids(flow_ids) if flow_ids else {}
    flows = [by_id[fid] for fid in flow_ids if fid in by_id]
    return deepseek._build_flow_context(flows)


def _make_title(flows: list[dict]) -> str:
    """Generate title from flow list."""
    if not flows:
        return "Empty analysis"
    first = flows[0]
    url = first.get("url", "")
    if len(url) > 50:
        url = url[:50] + "..."
    return f"{first.get('method', '')} {url}"


@router.post("/ai/analyze")
async def analyze(body: AnalyzeRequest):
    """Analyze specified flows, create chat record, return analysis result + chat_id.

    If no flow_ids (free chat), do not call API, only create empty chat record.
    """
    by_id = db.get_flows_by_ids(body.flow_ids) if body.flow_ids else {}
    flows = [by_id[fid] for fid in body.flow_ids if fid in by_id]

    flow_context = _build_flow_context(body.flow_ids)
    title = _make_title(flows) if flows else "Free chat"

    # Free chat: create empty chat record
    if not body.flow_ids:
        chat_id = db.create_ai_chat(title, body.flow_ids, flow_context)
        return ok({
            "chat_id": chat_id,
            "result": "",
            "title": title,
            "tool_results": [],
        })

    # With flows: call AI analysis
    result = await asyncio.to_thread(deepseek.analyze_flows, body.flow_ids)
    if not result.get("ok"):
        return err(result.get("error", "Analysis failed"))

    ai_reply = result["result"]

    # Create chat record
    chat_id = db.create_ai_chat(title, body.flow_ids, flow_context)

    # Record usage
    service = deepseek._get_current_service()
    model = deepseek._get_model(service)
    if result.get("input_tokens") or result.get("output_tokens"):
        deepseek._record_usage(
            chat_id=chat_id,
            service=service,
            model=model,
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
        )

    # Store AI's first analysis reply
    db.add_ai_message(chat_id, "assistant", ai_reply)

    return ok({
        "chat_id": chat_id,
        "result": ai_reply,
        "title": title,
        "tool_results": result.get("tool_results", []),
        "usage": {
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
        },
    })


@router.post("/ai/chat")
async def chat(body: ChatRequest):
    """Multi-turn chat with SSE streaming support."""
    chat_record = db.get_ai_chat(body.chat_id)
    if not chat_record:
        return err("Chat record not found")

    if not body.message.strip():
        return err("Message cannot be empty")

    # Get history messages
    history = db.get_ai_messages(body.chat_id)
    history_list = [{"role": m["role"], "content": m["content"]} for m in history]

    # Flow context
    flow_context = chat_record["flow_context"] or ""
    if body.flow_ids:
        flow_context = _build_flow_context(body.flow_ids)

    # Store user message
    db.add_ai_message(body.chat_id, "user", body.message)

    # Call AI
    result = await asyncio.to_thread(
        deepseek.chat,
        history_list,
        flow_context,
        body.message,
    )
    if not result.get("ok"):
        return err(result.get("error", "Chat failed"))

    ai_reply = result["result"]

    # Record usage
    service = deepseek._get_current_service()
    model = deepseek._get_model(service)
    if result.get("input_tokens") or result.get("output_tokens"):
        deepseek._record_usage(
            chat_id=body.chat_id,
            service=service,
            model=model,
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
        )

    # Store AI reply
    db.add_ai_message(body.chat_id, "assistant", ai_reply)

    return ok({
        "result": ai_reply,
        "tool_results": result.get("tool_results", []),
        "usage": {
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
        },
    })


@router.post("/ai/chat/stream")
async def chat_stream(body: ChatRequest):
    """Streaming chat endpoint using SSE."""
    chat_record = db.get_ai_chat(body.chat_id)
    if not chat_record:
        return err("Chat record not found")

    if not body.message.strip():
        return err("Message cannot be empty")

    # Get history
    history = db.get_ai_messages(body.chat_id)
    history_list = [{"role": m["role"], "content": m["content"]} for m in history]

    # Add current user message
    history_list.append({"role": "user", "content": body.message})

    # Flow context
    flow_context = chat_record["flow_context"] or ""
    if body.flow_ids:
        flow_context = _build_flow_context(body.flow_ids)

    # Store user message
    db.add_ai_message(body.chat_id, "user", body.message)

    async def generate():
        service = deepseek._get_current_service()
        model = deepseek._get_model(service)

        # Run non-streaming API call in thread
        result = await asyncio.to_thread(
            deepseek._call_api,
            history_list,
            use_tools=True,
        )

        if not result.get("ok"):
            err_data = json.dumps({"error": result.get("error", "Unknown error")})
            yield f"data: {err_data}\n\n"
            yield "data: [DONE]\n\n"
            return

        ai_reply = result["result"]

        # Record usage
        if result.get("input_tokens") or result.get("output_tokens"):
            deepseek._record_usage(
                chat_id=body.chat_id,
                service=service,
                model=model,
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
            )

        # Store AI reply
        db.add_ai_message(body.chat_id, "assistant", ai_reply)

        # Send usage first
        usage_payload = json.dumps({
            "type": "usage",
            "usage": {
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
            }
        })
        yield f"data: {usage_payload}\n\n"

        # Stream content token by token (whitespace/newlines preserved;
        # json.dumps escapes real newlines so each SSE data line stays single-line)
        tokens = [t for t in re.split(r"(\s+)", ai_reply) if t]
        for tok in tokens:
            content_payload = json.dumps({"type": "content", "content": tok}, ensure_ascii=False)
            yield f"data: {content_payload}\n\n"
            await asyncio.sleep(0.005)

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ai/chats")
async def list_chats():
    """Chat record list."""
    chats = db.get_ai_chats()
    for c in chats:
        try:
            c["flow_ids"] = json.loads(c.get("flow_ids", "[]"))
        except Exception as e:
            _capture_log("error", "API exception in ai.py", extra={"exc": repr(e)})
            c["flow_ids"] = []
    return ok(chats)


@router.get("/ai/chats/{chat_id}")
async def get_chat(chat_id: int):
    """Get single chat record + all messages."""
    chat_record = db.get_ai_chat(chat_id)
    if not chat_record:
        return err("Chat record not found")
    try:
        chat_record["flow_ids"] = json.loads(chat_record.get("flow_ids", "[]"))
    except Exception as e:
        _capture_log("error", "API exception in ai.py", extra={"exc": repr(e)})
        chat_record["flow_ids"] = []
    messages = db.get_ai_messages(chat_id)
    return ok({"chat": chat_record, "messages": messages})


@router.put("/ai/chats/{chat_id}/title")
async def update_title(chat_id: int, body: UpdateTitleRequest):
    """Update chat title."""
    chat_record = db.get_ai_chat(chat_id)
    if not chat_record:
        return err("Chat record not found")
    db.update_ai_chat_title(chat_id, body.title)
    return ok({"title": body.title})


@router.delete("/ai/chats/{chat_id}")
async def delete_chat(chat_id: int):
    """Delete chat record."""
    db.delete_ai_chat(chat_id)
    return ok({"deleted": True})


# ---------- AI Service Management ----------

@router.get("/ai/services")
async def get_services():
    """Get available AI services and their status."""
    status = deepseek.get_service_status()
    return ok(status)


@router.get("/ai/usage")
async def get_usage():
    """Get AI usage statistics."""
    stats = deepseek.get_ai_usage_stats()
    return ok(stats)


@router.put("/ai/service")
async def update_service(body: UpdateServiceRequest):
    """Update the current AI service."""
    service = body.service
    if service not in deepseek.AI_SERVICES:
        return err(f"Unknown service: {service}")
    db.set_setting("ai_service", service)
    return ok({"service": service})


@router.put("/ai/model")
async def update_model(body: UpdateModelRequest):
    """Update the model for a specific service."""
    service = body.service
    model = body.model
    if service not in deepseek.AI_SERVICES:
        return err(f"Unknown service: {service}")
    config = deepseek.AI_SERVICES[service]
    if model not in config.get("supported_models", []):
        return err(f"Model {model} not supported for service {service}")
    db.set_setting(f"{service}_model", model)
    return ok({"service": service, "model": model})


@router.get("/ai/models/{service}")
async def get_models(service: str):
    """Get available models for a service."""
    if service not in deepseek.AI_SERVICES:
        return err(f"Unknown service: {service}")
    config = deepseek.AI_SERVICES[service]
    return ok({
        "service": service,
        "name": config["name"],
        "models": config.get("supported_models", []),
        "default_model": config.get("default_model", ""),
        "pricing": config.get("pricing", {}),
    })
