"""压容回收：压容与超额占位回收同成败；双人同压只占档锁记争用。

口径（封入压容回收卡，当天禁止改口）：
  甲路径 = 事务内改小容量并作废超额占位，任一步失败则容量与占位同滚。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import connect, init_db  # noqa: E402
import db as dbmod  # noqa: E402

FIXTURE = json.loads((ROOT / "fixtures" / "shrink_case.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _place_holds(c, h):
    """按夹具把超额占位挂进档期（此时容量尚未下压，占位全部合法）。"""
    code = FIXTURE["slot_code"]
    for extra in FIXTURE["extra_holds"]:
        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": code, "party_key": extra["party_key"], "seats": extra["seats"]},
        )
        assert r.status_code == 200 and r.json()["disposition"] == "占位", r.text
    return code


def _open_holds(c, h, code):
    items = c.get("/api/holds", headers=h).json()["items"]
    return [x for x in items if x["slot_code"] == code]


def test_fixture_dumb_shrink_leaves_excess_holds_false_green():
    """夹具假绿：旧链路只改容量数字，超额占位仍挂，对账只看正式册故灯绿。"""
    with TestClient(app) as c:
        h = _login(c)
        code = _place_holds(c, h)
        # 旧链路症结：缩容只管改数字（直写库，不动占位）
        with connect() as conn:
            conn.execute(
                "UPDATE slots SET capacity=? WHERE code=?", (FIXTURE["shrink_to"], code)
            )
            conn.commit()
        open_seats = sum(x["seats"] for x in _open_holds(c, h, code) if x["status"] == "占位中")
        assert open_seats > FIXTURE["shrink_to"]  # 超额占位仍挂
        recon = [
            i for i in c.get("/api/reconcile", headers=h).json()["items"]
            if i["slot_code"] == code
        ][0]
        # 假绿：正式册对得上，灯就绿，超额占位被无视
        assert recon["slot_booked"] == recon["booking_seats"]


def test_shrink_reclaims_excess_holds_atomically():
    """压容未回收即失败：容量、超额占位、余席、对账灯、审计条五处须同态。"""
    with TestClient(app) as c:
        h = _login(c)
        code = _place_holds(c, h)
        r = c.post(
            "/api/slots/shrink",
            headers=h,
            json={"code": code, "new_capacity": FIXTURE["shrink_to"], "path": "甲"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["path"] == "甲"
        # 1) 容量数字
        slot = c.get(f"/api/slots/{code}", headers=h).json()
        assert slot["capacity"] == FIXTURE["shrink_to"]
        # 2) 超额占位已回收（最新先收），未超额的保留
        holds = _open_holds(c, h, code)
        reclaimed = {x["party_key"] for x in holds if x["status"] == "已回收"}
        assert reclaimed == set(FIXTURE["expect_reclaim_parties"])
        still_open = {x["party_key"] for x in holds if x["status"] == "占位中"}
        assert still_open == set(FIXTURE["expect_keep_parties"])
        # 3) 余席同态：在占 ≤ 新容量 − 已订；余 1 席可再占、2 席进候补
        open_seats = sum(x["seats"] for x in holds if x["status"] == "占位中")
        assert open_seats <= slot["capacity"] - slot["booked"]
        probe = c.post(
            "/api/holds", headers=h,
            json={"slot_code": code, "party_key": "P-PROBE", "seats": 1},
        )
        assert probe.json()["disposition"] == "占位"
        over = c.post(
            "/api/holds", headers=h,
            json={"slot_code": code, "party_key": "P-PROBE2", "seats": 2},
        )
        assert over.json()["disposition"] == "候补"
        # 4) 对账灯同态：无超额悬挂，灯才绿
        recon = [
            i for i in c.get("/api/reconcile", headers=h).json()["items"]
            if i["slot_code"] == code
        ][0]
        assert recon["ok"] is True
        assert recon["overflow"] == 0
        # 5) 审计条同态：压容与逐笔回收均有条
        audit = c.get("/api/audit", headers=h).json()["items"]
        assert any(a["action"] == "slot_shrink" and a["target"] == code for a in audit)
        reclaim_logs = [a for a in audit if a["action"] == "hold_reclaim"]
        assert len(reclaim_logs) == len(FIXTURE["expect_reclaim_parties"])
        # 压容回收卡：口径封卡
        cards = c.get("/api/shrink/cards", headers=h).json()["items"]
        assert cards and cards[0]["path"] == "甲"
        assert cards[0]["slot_code"] == code


def test_shrink_same_slot_contention_latecomer_only_logs():
    """双人同压同一档：先者占档锁，后到只记争用，不写业务。"""
    with TestClient(app) as c:
        h_admin = _login(c, "admin")
        h_clerk = _login(c, "clerk")
        code = FIXTURE["slot_code"]
        r = c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": f"slot:{code}"})
        assert r.status_code == 200, r.text
        before = c.get(f"/api/slots/{code}", headers=h_admin).json()
        r2 = c.post(
            "/api/slots/shrink",
            headers=h_clerk,
            json={"code": code, "new_capacity": FIXTURE["shrink_to"]},
        )
        assert r2.status_code == 409, r2.text
        after = c.get(f"/api/slots/{code}", headers=h_admin).json()
        assert after["capacity"] == before["capacity"]  # 后到不改容量
        holds = _open_holds(c, h_admin, code)
        assert all(x["status"] == "占位中" for x in holds)  # 后到不动占位
        cont = c.get("/api/contention", headers=h_admin).json()["items"]
        assert any(
            x["lock_key"] == f"slot:{code}" and x["actor"] == "clerk" for x in cont
        )  # 只记争用
        audit = c.get("/api/audit", headers=h_admin).json()["items"]
        assert not any(a["action"] == "slot_shrink" for a in audit)


def test_shrink_path_sealed_same_day_no_switch():
    """压容回收卡当天禁止改口：封甲后来乙，拒写并记偏。"""
    with TestClient(app) as c:
        h = _login(c)
        code = FIXTURE["slot_code"]
        r1 = c.post(
            "/api/slots/shrink", headers=h,
            json={"code": code, "new_capacity": 6, "path": "甲"},
        )
        assert r1.status_code == 200, r1.text
        r2 = c.post(
            "/api/slots/shrink", headers=h,
            json={"code": code, "new_capacity": 5, "path": "乙"},
        )
        assert r2.status_code == 409, r2.text
        slot = c.get(f"/api/slots/{code}", headers=h).json()
        assert slot["capacity"] == 6  # 拒写：容量不动
        audit = c.get("/api/audit", headers=h).json()["items"]
        assert any(
            a["action"] == "shrink_deviation" and a["target"] == code for a in audit
        )  # 记偏
