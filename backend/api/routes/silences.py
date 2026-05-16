"""Silence endpoints. Matchers are arbitrary dicts (later validated against
allowed alarm keys: check_id / target / stage / severity).

Reads (GET) are anonymous. Create / delete require login; the creator's
identity comes from their session, not the request body.
"""
from __future__ import annotations
from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Body

from ... import auth as A
from .auth import require_user

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
async def create_silence(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
    body: dict = Body(...),
):
    required = {"id", "matchers", "starts", "ends"}
    missing = required - set(body)
    if missing:
        raise HTTPException(400, f"missing fields: {sorted(missing)}")
    pool = request.app.state.store.pool
    await request.app.state.store.create_silence(
        sid=body["id"],
        matchers=body["matchers"],
        starts=datetime.fromisoformat(body["starts"]),
        ends=datetime.fromisoformat(body["ends"]),
        reason=body.get("reason"),
        created_by=user["email"],
    )
    await A.audit(pool, user_email=user["email"], action="silence.create",
                  target=f"silence:{body['id']}",
                  payload={"matchers": body["matchers"], "reason": body.get("reason"),
                           "starts": body["starts"], "ends": body["ends"]})
    return {"ok": True, "id": body["id"]}


@router.delete("/{sid}")
async def delete_silence(
    sid: str, request: Request,
    user: Annotated[dict, Depends(require_user)],
):
    pool = request.app.state.store.pool
    await request.app.state.store.delete_silence(sid)
    await A.audit(pool, user_email=user["email"], action="silence.delete",
                  target=f"silence:{sid}")
    return {"ok": True}
