"""AI 分析 API：分析流量 + 多轮对话 + 记录管理。"""

import asyncio
import json

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..ai import deepseek
from . import err, ok

router = APIRouter()


class AnalyzeRequest(BaseModel):
    flow_ids: list[int] = []


class ChatRequest(BaseModel):
    chat_id: int
    message: str
    # 可选：本次对话追加引用的流量 ID（如从抓包页"发送到已有会话"）
    flow_ids: list[int] = []


class UpdateTitleRequest(BaseModel):
    title: str


def _build_flow_context(flow_ids: list[int]) -> str:
    """从 flow_ids 构建流量上下文文本（按 id 批量查询，消除 N+1）。"""
    by_id = db.get_flows_by_ids(flow_ids)
    flows = [by_id[fid] for fid in flow_ids if fid in by_id]
    return deepseek._build_flow_context(flows)


def _make_title(flows: list[dict]) -> str:
    """从流量列表生成标题。"""
    if not flows:
        return "空分析"
    first = flows[0]
    url = first.get("url", "")
    if len(url) > 50:
        url = url[:50] + "..."
    return f"{first.get('method', '')} {url}"


@router.post("/ai/analyze")
async def analyze(body: AnalyzeRequest):
    """分析指定流量，创建聊天记录，返回分析结果 + chat_id。

    无 flow_ids（自由对话）时不调用 API，只创建空聊天记录，
    等用户通过 /ai/chat 发首条消息再调用，避免浪费 token 生成问候语。
    """
    by_id = db.get_flows_by_ids(body.flow_ids) if body.flow_ids else {}
    flows = [by_id[fid] for fid in body.flow_ids if fid in by_id]

    flow_context = _build_flow_context(body.flow_ids)
    title = _make_title(flows) if flows else "自由对话"

    # 自由对话：不调 API，只创建聊天记录，等用户先说话
    if not body.flow_ids:
        chat_id = db.create_ai_chat(title, body.flow_ids, flow_context)
        return ok({
            "chat_id": chat_id,
            "result": "",
            "title": title,
            "tool_results": [],
        })

    # 有流量：调用 AI 分析（同步阻塞调用移到线程池，避免阻塞事件循环）
    result = await asyncio.to_thread(deepseek.analyze_flows, body.flow_ids)
    if not result.get("ok"):
        return err(result.get("error", "分析失败"))

    ai_reply = result["result"]

    # 持久化：创建聊天记录
    chat_id = db.create_ai_chat(title, body.flow_ids, flow_context)
    # 存入 AI 的首次分析回复
    db.add_ai_message(chat_id, "assistant", ai_reply)

    return ok({
        "chat_id": chat_id,
        "result": ai_reply,
        "title": title,
        "tool_results": result.get("tool_results", []),
    })


@router.post("/ai/chat")
async def chat(body: ChatRequest):
    """多轮对话：基于已有聊天记录追问。

    支持在消息中追加引用流量（flow_ids）：当用户从抓包页"发送到已有会话"
    时，前端会把引用的流量 ID 一起传过来，后端构建流量上下文注入给 AI。
    """
    chat = db.get_ai_chat(body.chat_id)
    if not chat:
        return err("聊天记录不存在")

    if not body.message.strip():
        return err("消息不能为空")

    # 获取历史消息（排除当前用户消息）
    history = db.get_ai_messages(body.chat_id)
    history_list = [{"role": m["role"], "content": m["content"]} for m in history]

    # 流量上下文：优先用本次追加的 flow_ids，否则用聊天记录原有的
    flow_context = chat["flow_context"] or ""
    if body.flow_ids:
        flow_context = _build_flow_context(body.flow_ids)

    # 调用 AI（同步阻塞调用移到线程池，避免阻塞事件循环）
    result = await asyncio.to_thread(
        deepseek.chat,
        history_list,
        flow_context,
        body.message,
    )
    if not result.get("ok"):
        return err(result.get("error", "对话失败"))

    ai_reply = result["result"]

    # 持久化：存入用户消息 + AI 回复
    db.add_ai_message(body.chat_id, "user", body.message)
    db.add_ai_message(body.chat_id, "assistant", ai_reply)

    return ok({"result": ai_reply, "tool_results": result.get("tool_results", [])})


@router.get("/ai/chats")
async def list_chats():
    """聊天记录列表。"""
    chats = db.get_ai_chats()
    # 解析 flow_ids
    for c in chats:
        try:
            c["flow_ids"] = json.loads(c.get("flow_ids", "[]"))
        except Exception:  # noqa: BLE001
            c["flow_ids"] = []
    return ok(chats)


@router.get("/ai/chats/{chat_id}")
async def get_chat(chat_id: int):
    """获取单个聊天记录 + 所有消息。"""
    chat = db.get_ai_chat(chat_id)
    if not chat:
        return err("聊天记录不存在")
    try:
        chat["flow_ids"] = json.loads(chat.get("flow_ids", "[]"))
    except Exception:  # noqa: BLE001
        chat["flow_ids"] = []
    messages = db.get_ai_messages(chat_id)
    return ok({"chat": chat, "messages": messages})


@router.put("/ai/chats/{chat_id}/title")
async def update_title(chat_id: int, body: UpdateTitleRequest):
    """更新聊天标题。"""
    chat = db.get_ai_chat(chat_id)
    if not chat:
        return err("聊天记录不存在")
    db.update_ai_chat_title(chat_id, body.title)
    return ok({"title": body.title})


@router.delete("/ai/chats/{chat_id}")
async def delete_chat(chat_id: int):
    """删除聊天记录。"""
    db.delete_ai_chat(chat_id)
    return ok({"deleted": True})
