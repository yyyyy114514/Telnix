"""§3.13 Flow grouping: manage multiple flows in groups.

Endpoints:
- POST /flows/groups          Create group
- GET  /flows/groups          List groups
- GET  /flows/groups/{id}     View group (with flow details)
- DELETE /flows/groups/{id}   Delete group
- PUT  /flows/groups/{id}     Update group (name / flow_ids)
"""

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


class CreateGroupBody(BaseModel):
    """Create group."""
    name: str
    flow_ids: list[int] = []


class UpdateGroupBody(BaseModel):
    """Update group. Any field being None means no update."""
    name: str | None = None
    flow_ids: list[int] | None = None


def _parse_flow_ids(flow_ids_str: str) -> list[int]:
    """Parse comma-separated flow_ids string into int list."""
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
    """Serialize group. When include_flows=True, includes each flow's details."""
    out = dict(group)
    out["flow_ids"] = _parse_flow_ids(group.get("flow_ids", ""))
    if include_flows:
        by_id = db.get_flows_by_ids(out["flow_ids"])
        flows = [by_id[fid] for fid in out["flow_ids"] if fid in by_id]
        out["flows"] = flows
    return out


@router.post("/flows/groups")
async def create_group(body: CreateGroupBody):
    """Create flow group."""
    if not body.name:
        return err("name cannot be empty")
    group_id = db.create_flow_group(body.name, body.flow_ids)
    group = db.get_flow_group(group_id)
    return ok(_serialize_group(group))


@router.get("/flows/groups")
async def list_groups():
    """List all flow groups."""
    groups = db.get_flow_groups()
    return ok([_serialize_group(g) for g in groups])


@router.get("/flows/groups/{group_id}")
async def get_group(group_id: int):
    """View group details (including each flow's details)."""
    group = db.get_flow_group(group_id)
    if not group:
        return err("Group not found")
    return ok(_serialize_group(group, include_flows=True))


@router.put("/flows/groups/{group_id}")
async def update_group(group_id: int, body: UpdateGroupBody):
    """Update group."""
    group = db.get_flow_group(group_id)
    if not group:
        return err("Group not found")
    db.update_flow_group(group_id, name=body.name, flow_ids=body.flow_ids)
    return ok(_serialize_group(db.get_flow_group(group_id)))


@router.delete("/flows/groups/{group_id}")
async def delete_group(group_id: int):
    """Delete group (does not delete flows inside the group)."""
    group = db.get_flow_group(group_id)
    if not group:
        return err("Group not found")
    db.delete_flow_group(group_id)
    return ok({"deleted": True, "group_id": group_id})
