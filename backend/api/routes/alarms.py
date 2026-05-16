"""Alarm + ack + notification endpoints. Admin gating is wired later — for
v1 we trust the request body's ``user`` field and audit it."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request, Body

router = APIRouter(prefix="/api/alarms")


def _ser(row: dict) -> dict:
    out = dict(row)
    for k in ("opened_at", "closed_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    return out


@router.get("")
async def list_alarms(request: Request, status: str = "open", limit: int = 200):
    if status not in ("open", "closed", "all"):
        raise HTTPException(400, "status must be open|closed|all")
    rows = await request.app.state.store.list_alarms(status=status, limit=limit)
    return [_ser(r) for r in rows]


@router.get("/{alarm_id}")
async def get_alarm(alarm_id: int, request: Request):
    row = await request.app.state.store.fetch_alarm(alarm_id)
    if not row:
        raise HTTPException(404, "no such alarm")
    out = _ser(row)
    out["ack"] = await request.app.state.store.ack_state(alarm_id)
    out["notifications"] = [
        {**n, "sent_at": n["sent_at"].isoformat()}
        for n in await request.app.state.store.list_notifications(alarm_id)
    ]
    return out


@router.post("/{alarm_id}/ack")
async def ack(alarm_id: int, request: Request, body: dict = Body(...)):
    user = body.get("user") or "anonymous"
    note = body.get("note")
    row = await request.app.state.store.fetch_alarm(alarm_id)
    if not row:
        raise HTTPException(404, "no such alarm")
    await request.app.state.store.ack_alarm(alarm_id, user=user, note=note)
    return {"ok": True, "alarm_id": alarm_id, "user": user}


@router.post("/{alarm_id}/unack")
async def unack(alarm_id: int, request: Request):
    row = await request.app.state.store.fetch_alarm(alarm_id)
    if not row:
        raise HTTPException(404, "no such alarm")
    await request.app.state.store.unack_alarm(alarm_id)
    return {"ok": True, "alarm_id": alarm_id}
