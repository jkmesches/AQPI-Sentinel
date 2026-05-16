"""Silence endpoints. Matchers are arbitrary dicts (later validated against
allowed alarm keys: check_id / target / stage / severity)."""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Body

router = APIRouter(prefix="/api/silences")


def _ser(row: dict) -> dict:
    out = dict(row)
    for k in ("starts", "ends", "created_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    return out


@router.get("")
async def list_silences(request: Request, active_only: bool = False):
    store = request.app.state.store
    if active_only:
        rows = await store.list_active_silences(datetime.now().astimezone())
    else:
        rows = await store.list_silences()
    return [_ser(r) for r in rows]


@router.post("")
async def create_silence(request: Request, body: dict = Body(...)):
    required = {"id", "matchers", "starts", "ends"}
    missing = required - set(body)
    if missing:
        raise HTTPException(400, f"missing fields: {sorted(missing)}")
    await request.app.state.store.create_silence(
        sid=body["id"],
        matchers=body["matchers"],
        starts=datetime.fromisoformat(body["starts"]),
        ends=datetime.fromisoformat(body["ends"]),
        reason=body.get("reason"),
        created_by=body.get("created_by") or "anonymous",
    )
    return {"ok": True, "id": body["id"]}


@router.delete("/{sid}")
async def delete_silence(sid: str, request: Request):
    await request.app.state.store.delete_silence(sid)
    return {"ok": True}
