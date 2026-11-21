"""对账：档期 booked 与正式册 seats 同源校验，超额占位悬挂即灯红。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _overflow(conn, slot) -> dict:
    """同态口径：正式册对得上且在占不超容，灯才绿。"""
    booked_sum = conn.execute(
        """SELECT COALESCE(SUM(seats),0) s FROM bookings
           WHERE slot_id=? AND status='已确认'""",
        (slot["id"],),
    ).fetchone()["s"]
    open_hold_seats = conn.execute(
        "SELECT COALESCE(SUM(seats),0) s FROM holds WHERE slot_id=? AND status='占位中'",
        (slot["id"],),
    ).fetchone()["s"]
    overflow = max(0, int(slot["booked"]) + int(open_hold_seats) - int(slot["capacity"]))
    return {
        "booking_seats": booked_sum,
        "open_hold_seats": open_hold_seats,
        "free": int(slot["capacity"]) - int(slot["booked"]) - int(open_hold_seats),
        "overflow": overflow,
        "ok": int(booked_sum) == int(slot["booked"]) and overflow == 0,
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = []
        for s in slots:
            calc = _overflow(conn, s)
            items.append(
                {
                    "slot_code": s["code"],
                    "slot_booked": s["booked"],
                    "booking_seats": calc["booking_seats"],
                    "open_hold_seats": calc["open_hold_seats"],
                    "free": calc["free"],
                    "overflow": calc["overflow"],
                    "generation": s["generation"],
                    "ok": calc["ok"],
                    "status": s["status"],
                }
            )
        return {"items": items, "all_ok": all(i["ok"] for i in items)}


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        calc = _overflow(conn, s)
        holds = conn.execute(
            "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
            (s["id"],),
        ).fetchone()["c"]
        return {
            "slot_code": code,
            "slot_booked": s["booked"],
            "booking_seats": calc["booking_seats"],
            "open_holds": holds,
            "open_hold_seats": calc["open_hold_seats"],
            "free": calc["free"],
            "overflow": calc["overflow"],
            "ok": calc["ok"],
            "generation": s["generation"],
        }
