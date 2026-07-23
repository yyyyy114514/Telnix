"""§3.13 流量分组：将多条流量归组管理。

端点：
- POST /flows/groups          创建分组
- GET  /flows/groups          列出分组
- GET  /flows/groups/{id}     查看分组（含 flow 详情）
- DELETE /flows/groups/{id}   删除分组
- PUT  /flows/groups/{id}     更新分组（name / flow_ids）
"""

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from . import err, ok

router = APIRouter()


class CreateGroupBody(BaseModel):
    """创建分组。"""
    name: str
    flow_ids: list[int] = []


class UpdateGroupBody(BaseModel):
    """更新分组。任一字段为 None 表示不更新。"""
    name: str | None = None
    flow_ids: list[int] | None = None


def _parse_flow_ids(flow_ids_str: str) -> list[int]:
    """把逗号分隔的 flow_ids 字符串解析为 int 列表。"""
    if not flow_ids_str:
        return []
    out = []
    for part in flow_ids_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _serialize_group(group: dict, include_flows: bool = False) -> dict:
    """序列化分组。include_flows=True 时附带每条 flow 的详情。"""
    out = dict(group)
    out["flow_ids"] = _parse_flow_ids(group.get("flow_ids", ""))
    if include_flows:
        flows = []
        for fid in out["flow_ids"]:
            flow = db.get_flow(fid)
            if flow:
                flows.append(flow)
        out["flows"] = flows
    return out


@router.post("/flows/groups")
async def create_group(body: CreateGroupBody):
    """创建流量分组。"""
    if not body.name:
        return err("name 不能为空")
    group_id = db.create_flow_group(body.name, body.flow_ids)
    group = db.get_flow_group(group_id)
    return ok(_serialize_group(group))


@router.get("/flows/groups")
async def list_groups():
    """列出所有流量分组。"""
    groups = db.get_flow_groups()
    return ok([_serialize_group(g) for g in groups])


@router.get("/flows/groups/{group_id}")
async def get_group(group_id: int):
    """查看分组详情（含每条 flow 的详情）。"""
    group = db.get_flow_group(group_id)
    if not group:
        return err("分组不存在")
    return ok(_serialize_group(group, include_flows=True))


@router.put("/flows/groups/{group_id}")
async def update_group(group_id: int, body: UpdateGroupBody):
    """更新分组。"""
    group = db.get_flow_group(group_id)
    if not group:
        return err("分组不存在")
    db.update_flow_group(group_id, name=body.name, flow_ids=body.flow_ids)
    return ok(_serialize_group(db.get_flow_group(group_id)))


@router.delete("/flows/groups/{group_id}")
async def delete_group(group_id: int):
    """删除分组（不删除组内的流量）。"""
    group = db.get_flow_group(group_id)
    if not group:
        return err("分组不存在")
    db.delete_flow_group(group_id)
    return ok({"deleted": True, "group_id": group_id})
