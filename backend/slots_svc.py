"""档期盘：开档、关档、压容回收、容量查询。"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from hold_svc import reclaim_excess_holds
from locks import release, require_lock, try_acquire

router = APIRouter(tags=["slots"])

# 压容回收卡：正确性口径只走甲路径（事务内整笔回收），封卡后当天禁止改口，
# 乙路径（核销影子切换）永不与甲混用，来乙即偏离，拒写并记偏。
SEALED_PATH = "甲"

SHRINK_CARD_DDL = """
CREATE TABLE IF NOT EXISTS shrink_cards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_code TEXT NOT NULL,
  sealed_date TEXT NOT NULL,
  path TEXT NOT NULL,
  old_capacity INTEGER NOT NULL,
  new_capacity INTEGER NOT NULL,
  reclaimed_holds INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
)
"""


class SlotIn(BaseModel):
    code: str
    title: str
    start_at: str
    end_at: str
    capacity: int = Field(ge=1, le=500)


class CloseIn(BaseModel):
    code: str


class ShrinkIn(BaseModel):
    code: str
    new_capacity: int = Field(ge=1, le=500)
    path: str = SEALED_PATH


@router.get("/api/slots")
def list_slots(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM slots ORDER BY start_at, code"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/slots/{code}")
def get_slot(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, "档期不存在")
        return dict(row)


@router.post("/api/slots")
def create_slot(body: SlotIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            exists = conn.execute(
                "SELECT id FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if exists:
                raise HTTPException(409, "档期编码已存在")
            if body.end_at <= body.start_at:
                raise HTTPException(400, "结束时间必须晚于开始")
            conn.execute(
                """INSERT INTO slots(code,title,start_at,end_at,capacity,booked,status,generation)
                   VALUES (?,?,?,?,?,0,'开放',1)""",
                (body.code, body.title, body.start_at, body.end_at, body.capacity),
            )
            write_audit(conn, user, "slot_create", body.code, body.model_dump())
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True, "code": body.code}


@router.post("/api/slots/close")
def close_slot(body: CloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, f"slot:{body.code}", user)
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if row["status"] == "关闭":
                raise HTTPException(409, "档期已关闭")
            open_holds = conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
                (row["id"],),
            ).fetchone()["c"]
            if open_holds:
                raise HTTPException(409, "仍有未确认占位，禁止关档")
            conn.execute(
                "UPDATE slots SET status='关闭' WHERE id=?", (row["id"],)
            )
            write_audit(conn, user, "slot_close", body.code, {"generation": row["generation"]})
            conn.commit()
        finally:
            release(conn, f"slot:{body.code}", user)
            conn.commit()
    return {"ok": True}


@router.post("/api/slots/shrink")
def shrink_slot(body: ShrinkIn, authorization: AuthHeader = None):
    """压容：甲路径事务内整笔回收，容量与超额占位同成败，口径封卡当天禁止改口。"""
    user = require_writer(authorization)
    lock_key = f"slot:{body.code}"
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(SHRINK_CARD_DDL)
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 后到只落争用记录，不写业务
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            row = conn.execute(
                "SELECT * FROM slots WHERE code=?", (body.code,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "档期不存在")
            if row["status"] != "开放":
                raise HTTPException(409, "档期未开放，禁止压容")
            sealed = conn.execute(
                "SELECT path FROM shrink_cards WHERE sealed_date=? ORDER BY id LIMIT 1",
                (today,),
            ).fetchone()
            sealed_path = sealed["path"] if sealed else SEALED_PATH
            if body.path != sealed_path or body.path != SEALED_PATH:
                write_audit(
                    conn,
                    user,
                    "shrink_deviation",
                    body.code,
                    {
                        "wanted_path": body.path,
                        "sealed_path": sealed_path,
                        "date": today,
                        "reason": "偏离压容回收卡，当天禁止改口",
                    },
                )
                conn.commit()  # 记偏落库后拒写
                raise HTTPException(409, "偏离压容回收卡：拒写并记偏")
            old_cap = int(row["capacity"])
            booked = int(row["booked"])
            if body.new_capacity >= old_cap:
                raise HTTPException(400, "仅允许压容：新容量必须小于现容量")
            if body.new_capacity < booked:
                raise HTTPException(409, "新容量不得低于已确认席数")
            reclaimed = reclaim_excess_holds(
                conn, row["id"], body.new_capacity - booked, user
            )
            conn.execute(
                "UPDATE slots SET capacity=?, generation=generation+1 WHERE id=?",
                (body.new_capacity, row["id"]),
            )
            cur = conn.execute(
                """INSERT INTO shrink_cards(slot_code,sealed_date,path,old_capacity,
                                            new_capacity,reclaimed_holds,actor,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    body.code,
                    today,
                    SEALED_PATH,
                    old_cap,
                    body.new_capacity,
                    len(reclaimed),
                    user,
                    now,
                ),
            )
            write_audit(
                conn,
                user,
                "slot_shrink",
                body.code,
                {
                    "old_capacity": old_cap,
                    "new_capacity": body.new_capacity,
                    "reclaimed_hold_ids": [r["id"] for r in reclaimed],
                    "reclaimed_seats": sum(int(r["seats"]) for r in reclaimed),
                    "path": SEALED_PATH,
                    "card_id": cur.lastrowid,
                    "generation": int(row["generation"]) + 1,
                },
            )
            conn.commit()
            return {
                "ok": True,
                "code": body.code,
                "path": SEALED_PATH,
                "old_capacity": old_cap,
                "new_capacity": body.new_capacity,
                "reclaimed_hold_ids": [r["id"] for r in reclaimed],
                "reclaimed_seats": sum(int(r["seats"]) for r in reclaimed),
                "card_id": cur.lastrowid,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return {
            "slots_open": conn.execute(
                "SELECT COUNT(*) c FROM slots WHERE status='开放'"
            ).fetchone()["c"],
            "holds_open": conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE status='占位中'"
            ).fetchone()["c"],
            "bookings": conn.execute(
                "SELECT COUNT(*) c FROM bookings WHERE status='已确认'"
            ).fetchone()["c"],
            "waitlist": conn.execute(
                "SELECT COUNT(*) c FROM waitlist WHERE status='候补中'"
            ).fetchone()["c"],
            "now": datetime.now().isoformat(timespec="seconds"),
        }
