"""端到端与核心逻辑测试。"""
from __future__ import annotations

import copy
import math

import pytest
from fastapi.testclient import TestClient

from app.core import Curve
from app.main import create_app


@pytest.fixture()
def client():
    app = create_app(":memory:")
    with TestClient(app) as c:
        yield c


def base_payload() -> dict:
    """合法场景：S -B1(CB1)-> A -B2(CB2)-> B -B3-> C，另有 S -B4-> D（无断路器）。"""
    return {
        "name": "demo-feeder",
        "source": "S",
        "branches": [
            {"id": "B1", "from": "S", "to": "A",
             "breaker": {"id": "CB1", "curve_id": "C_UP"}},
            {"id": "B2", "from": "A", "to": "B",
             "breaker": {"id": "CB2", "curve_id": "C_MAIN"}},
            {"id": "B3", "from": "B", "to": "C"},
            {"id": "B4", "from": "S", "to": "D"},
        ],
        "curves": [
            {"id": "C_MAIN", "points": [
                {"current": 10, "time": 10},
                {"current": 100, "time": 1},
                {"current": 1000, "time": 0.1},
            ]},
            {"id": "C_UP", "points": [
                {"current": 10, "time": 20},
                {"current": 100, "time": 1.5},
                {"current": 1000, "time": 0.12},
            ]},
        ],
        "faults": [
            # 比值 @10:2.0 @100:1.5 @1000:1.2
            {"id": "F1", "branch_id": "B3",
             "current_min": 10, "current_max": 1000, "r": 1.8},
            {"id": "F2", "branch_id": "B3",
             "current_min": 10, "current_max": 1000, "r": 1.1},
            # 区间右端比值恰为 R（等号合格）
            {"id": "F3", "branch_id": "B2",
             "current_min": 10, "current_max": 100, "r": 1.5},
            # 有主保护而无上级 -> 合格
            {"id": "F4", "branch_id": "B1",
             "current_min": 10, "current_max": 1000, "r": 2.0},
            # 路径上无断路器 -> 无保护（区间无需曲线覆盖）
            {"id": "F5", "branch_id": "B4",
             "current_min": 5, "current_max": 5000, "r": 3.0},
        ],
    }


def create_ok(client: TestClient, payload: dict) -> str:
    resp = client.post("/scenarios", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# 基础流程
# ---------------------------------------------------------------------------

def test_create_and_get_roundtrip(client):
    payload = base_payload()
    sid = create_ok(client, payload)

    created = client.get(f"/scenarios/{sid}")
    assert created.status_code == 200
    body = created.json()
    assert body["id"] == sid
    assert body["name"] == "demo-feeder"
    assert body["created_at"]
    assert body["branches"][0]["from"] == "S"
    assert body["branches"][0]["to"] == "A"
    assert body["branches"][2]["breaker"] is None
    assert len(body["faults"]) == 5

    listing = client.get("/scenarios").json()
    assert len(listing) == 1
    assert listing[0]["id"] == sid
    assert listing[0]["branch_count"] == 4
    assert listing[0]["fault_count"] == 5


def test_root_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_scenario_is_immutable(client):
    sid = create_ok(client, base_payload())
    assert client.put(f"/scenarios/{sid}", json={}).status_code == 405
    assert client.patch(f"/scenarios/{sid}", json={}).status_code == 405
    assert client.delete(f"/scenarios/{sid}").status_code == 405
    # 重复提交相同内容只会创建新场景，不会覆盖
    other = create_ok(client, base_payload())
    assert other != sid


def test_unknown_scenario_404(client):
    assert client.get("/scenarios/nope").status_code == 404
    assert client.get("/scenarios/nope/verification").status_code == 404


def test_unknown_fault_404(client):
    sid = create_ok(client, base_payload())
    resp = client.get(f"/scenarios/{sid}/faults/NOPE/verification")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 核验结论
# ---------------------------------------------------------------------------

def test_verify_all_faults(client):
    sid = create_ok(client, base_payload())
    resp = client.get(f"/scenarios/{sid}/verification")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario_id"] == sid
    assert body["summary"] == {
        "total": 5, "qualified": 3, "unqualified": 1, "unprotected": 1,
    }
    reports = {f["fault_id"]: f for f in body["faults"]}

    # F1: 比值 2.0 -> 1.5 -> 1.2，R=1.8，违规范围 (cross, 1000]
    f1 = reports["F1"]
    assert f1["status"] == "unqualified"
    assert f1["main_breaker_id"] == "CB2"
    assert len(f1["pairs"]) == 1
    pair = f1["pairs"][0]
    assert pair["upstream_breaker_id"] == "CB1"
    assert pair["qualified"] is False
    assert pair["min_ratio"] == pytest.approx(1.2, rel=1e-9)
    assert pair["min_ratio_current"] == pytest.approx(1000.0)
    assert len(pair["violations"]) == 1
    v = pair["violations"][0]
    # 交点：ln 空间线性求根
    d0 = math.log(20) - math.log(10) - math.log(1.8)
    d1 = math.log(1.5) - math.log(1) - math.log(1.8)
    cross = math.exp(math.log(10) + (0 - d0) / (d1 - d0)
                     * (math.log(100) - math.log(10)))
    assert 23.2 < cross < 23.3
    assert v["current_from"] == pytest.approx(cross, rel=1e-9)
    assert v["current_to"] == pytest.approx(1000.0)
    assert v["from_included"] is False   # 交点处等号，合格
    assert v["to_included"] is True      # 区间右端比值 1.2 < 1.8

    # F2: R=1.1，处处合格，最小比值 1.2 @ 1000
    f2 = reports["F2"]
    assert f2["status"] == "qualified"
    assert f2["pairs"][0]["violations"] == []
    assert f2["pairs"][0]["min_ratio"] == pytest.approx(1.2, rel=1e-9)

    # F3: 区间右端比值恰等于 R，等号合格
    f3 = reports["F3"]
    assert f3["status"] == "qualified"
    assert f3["pairs"][0]["min_ratio"] == pytest.approx(1.5, rel=1e-9)
    assert f3["pairs"][0]["min_ratio_current"] == pytest.approx(100.0)
    assert f3["pairs"][0]["violations"] == []

    # F4: 有主保护而无上级 -> 合格
    f4 = reports["F4"]
    assert f4["status"] == "qualified"
    assert f4["main_breaker_id"] == "CB1"
    assert f4["pairs"] == []

    # F5: 路径上无断路器 -> 无保护
    f5 = reports["F5"]
    assert f5["status"] == "unprotected"
    assert f5["main_breaker_id"] is None
    assert f5["pairs"] == []


def test_verify_single_fault_matches_bulk(client):
    sid = create_ok(client, base_payload())
    bulk = client.get(f"/scenarios/{sid}/verification").json()
    single = client.get(f"/scenarios/{sid}/faults/F1/verification").json()
    assert single == next(f for f in bulk["faults"] if f["fault_id"] == "F1")


def test_multiple_upstreams_any_failure_fails_fault(client):
    payload = base_payload()
    payload["branches"] = [
        {"id": "L1", "from": "S", "to": "A",
         "breaker": {"id": "CB1", "curve_id": "C_SLOW"}},
        {"id": "L2", "from": "A", "to": "B",
         "breaker": {"id": "CB2", "curve_id": "C_MID"}},
        {"id": "L3", "from": "B", "to": "C",
         "breaker": {"id": "CB3", "curve_id": "C_FAST"}},
    ]
    payload["curves"] = [
        {"id": "C_FAST", "points": [
            {"current": 10, "time": 1}, {"current": 100, "time": 0.1}]},
        {"id": "C_MID", "points": [
            {"current": 10, "time": 2}, {"current": 100, "time": 0.2}]},
        {"id": "C_SLOW", "points": [
            {"current": 10, "time": 1.5}, {"current": 100, "time": 0.15}]},
    ]
    payload["faults"] = [
        {"id": "FX", "branch_id": "L3",
         "current_min": 10, "current_max": 100, "r": 1.8},
    ]
    sid = create_ok(client, payload)
    report = client.get(f"/scenarios/{sid}/faults/FX/verification").json()
    assert report["status"] == "unqualified"
    assert report["main_breaker_id"] == "CB3"
    pairs = {p["upstream_breaker_id"]: p for p in report["pairs"]}
    assert set(pairs) == {"CB2", "CB1"}
    # CB2 比值恒为 2.0 >= 1.8 合格；CB1 比值恒为 1.5 < 1.8 不合格
    assert pairs["CB2"]["qualified"] is True
    assert pairs["CB2"]["min_ratio"] == pytest.approx(2.0, rel=1e-9)
    assert pairs["CB1"]["qualified"] is False
    assert pairs["CB1"]["min_ratio"] == pytest.approx(1.5, rel=1e-9)
    v = pairs["CB1"]["violations"][0]
    assert (v["current_from"], v["current_to"]) == (10.0, 100.0)
    assert v["from_included"] is True and v["to_included"] is True


def test_min_ratio_tie_takes_smaller_current(client):
    payload = base_payload()
    payload["branches"] = [
        {"id": "L1", "from": "S", "to": "A",
         "breaker": {"id": "CB1", "curve_id": "C_UP2"}},
        {"id": "L2", "from": "A", "to": "B",
         "breaker": {"id": "CB2", "curve_id": "C_MAIN2"}},
    ]
    payload["curves"] = [
        {"id": "C_MAIN2", "points": [
            {"current": 10, "time": 2}, {"current": 100, "time": 1},
            {"current": 1000, "time": 0.5}]},
        {"id": "C_UP2", "points": [
            {"current": 10, "time": 4}, {"current": 100, "time": 2},
            {"current": 1000, "time": 1}]},
    ]
    payload["faults"] = [
        {"id": "FT", "branch_id": "L2",
         "current_min": 10, "current_max": 1000, "r": 1.5},
    ]
    sid = create_ok(client, payload)
    pair = client.get(
        f"/scenarios/{sid}/faults/FT/verification").json()["pairs"][0]
    # 比值恒为 2.0：并列最小取较小电流（区间下界 10）
    assert pair["min_ratio"] == pytest.approx(2.0, rel=1e-9)
    assert pair["min_ratio_current"] == pytest.approx(10.0)
    assert pair["qualified"] is True


def test_single_point_interval(client):
    payload = base_payload()
    payload["faults"] = [
        {"id": "FP", "branch_id": "B3",
         "current_min": 50, "current_max": 50, "r": 1.8},
    ]
    sid = create_ok(client, payload)
    report = client.get(f"/scenarios/{sid}/faults/FP/verification").json()
    pair = report["pairs"][0]
    # I=50 处比值 = 3.2712/2.0 ≈ 1.6356 < 1.8，单点违规
    assert report["status"] == "unqualified"
    assert pair["min_ratio"] == pytest.approx(1.6356, rel=1e-3)
    assert pair["min_ratio_current"] == pytest.approx(50.0)
    v = pair["violations"][0]
    assert v["current_from"] == v["current_to"] == 50.0
    assert v["from_included"] is True and v["to_included"] is True


# ---------------------------------------------------------------------------
# 校验：拓扑
# ---------------------------------------------------------------------------

def _expect_422(client, payload, fragment):
    resp = client.post("/scenarios", json=payload)
    assert resp.status_code == 422, resp.text
    assert fragment in resp.text


def test_reject_cycle(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "S", "to": "A"},
        {"id": "B2", "from": "A", "to": "B"},
        {"id": "B3", "from": "B", "to": "A"},  # 环 + A 入度 2
    ]
    p["faults"] = []
    _expect_422(client, p, "不是树")


def test_reject_diamond_two_parents(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "S", "to": "A"},
        {"id": "B2", "from": "S", "to": "B"},
        {"id": "B3", "from": "A", "to": "C"},
        {"id": "B4", "from": "B", "to": "C"},  # C 入度 2
    ]
    p["faults"] = []
    _expect_422(client, p, "入度")


def test_reject_disconnected(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "S", "to": "A"},
        {"id": "B2", "from": "X", "to": "Y"},  # 与电源不相连
    ]
    p["faults"] = []
    _expect_422(client, p, "不可达")


def test_reject_edge_into_source(client):
    p = base_payload()
    p["branches"] = [
        {"id": "B1", "from": "A", "to": "S"},  # 指向电源
        {"id": "B2", "from": "S", "to": "B"},
    ]
    p["faults"] = []
    _expect_422(client, p, "入边")


def test_reject_unknown_source(client):
    p = base_payload()
    p["source"] = "NOWHERE"
    _expect_422(client, p, "电源节点")


def test_reject_duplicate_ids(client):
    p = base_payload()
    p["branches"].append({"id": "B1", "from": "S", "to": "Z"})
    _expect_422(client, p, "支路 id 重复")

    p = base_payload()
    p["curves"].append(p["curves"][0])
    _expect_422(client, p, "曲线 id 重复")

    p = base_payload()
    p["faults"].append(p["faults"][0])
    _expect_422(client, p, "故障 id 重复")

    p = base_payload()
    p["branches"][2]["breaker"] = {"id": "CB1", "curve_id": "C_MAIN"}
    _expect_422(client, p, "断路器 id 重复")


def test_reject_unknown_curve_reference(client):
    p = base_payload()
    p["branches"][0]["breaker"]["curve_id"] = "GHOST"
    _expect_422(client, p, "不存在的曲线")


# ---------------------------------------------------------------------------
# 校验：曲线与故障
# ---------------------------------------------------------------------------

def test_reject_bad_curves(client):
    p = base_payload()
    p["curves"][0]["points"] = [{"current": 10, "time": 1}]
    _expect_422(client, p, "at least 2")

    p = base_payload()
    p["curves"][0]["points"][1]["current"] = 10  # 非严格递增
    _expect_422(client, p, "严格递增")

    p = base_payload()
    p["curves"][0]["points"][0]["time"] = 0
    _expect_422(client, p, "greater than 0")

    p = base_payload()
    p["curves"][0]["points"][0]["current"] = -5
    _expect_422(client, p, "greater than 0")


def test_reject_bad_faults(client):
    p = base_payload()
    p["faults"][0]["current_min"] = 2000
    p["faults"][0]["current_max"] = 100
    _expect_422(client, p, "下界")

    p = base_payload()
    p["faults"][0]["r"] = 1.0
    _expect_422(client, p, "greater than 1")

    p = base_payload()
    p["faults"][0]["current_min"] = 0
    _expect_422(client, p, "greater than 0")

    p = base_payload()
    p["faults"][0]["branch_id"] = "GHOST"
    _expect_422(client, p, "不存在的支路")


def test_reject_insufficient_curve_coverage(client):
    p = base_payload()
    p["faults"] = [
        {"id": "FC", "branch_id": "B3",
         "current_min": 5, "current_max": 1000, "r": 1.5},
    ]
    _expect_422(client, p, "覆盖不足")

    p = base_payload()
    p["faults"] = [
        {"id": "FC", "branch_id": "B3",
         "current_min": 10, "current_max": 2000, "r": 1.5},
    ]
    _expect_422(client, p, "覆盖不足")


def test_interval_equal_to_curve_range_is_covered(client):
    p = base_payload()
    p["faults"] = [
        {"id": "FC", "branch_id": "B3",
         "current_min": 10, "current_max": 1000, "r": 1.5},
    ]
    create_ok(client, p)  # 边界恰好等于曲线范围：允许（不外推）


# ---------------------------------------------------------------------------
# 核心算法单元测试
# ---------------------------------------------------------------------------

def test_log_log_interpolation_no_extrapolation():
    curve = Curve.from_points("X", [
        type("P", (), {"current": 10.0, "time": 10.0})(),
        type("P", (), {"current": 100.0, "time": 1.0})(),
    ])
    # 几何中点电流 -> 几何中点时间（对数-对数线性）
    mid = math.sqrt(10.0 * 100.0)
    t = math.exp(curve.log_time_at(math.log(mid)))
    assert t == pytest.approx(math.sqrt(10.0 * 1.0), rel=1e-12)
    # 端点
    assert math.exp(curve.log_time_at(math.log(10.0))) == pytest.approx(10.0)
    assert math.exp(curve.log_time_at(math.log(100.0))) == pytest.approx(1.0)


def test_persistence_across_restarts(tmp_path):
    db_file = str(tmp_path / "scenarios.db")
    with TestClient(create_app(db_file)) as c1:
        sid = create_ok(c1, base_payload())
    # 用同一数据库文件重建应用（模拟重启）
    with TestClient(create_app(db_file)) as c2:
        assert c2.get(f"/scenarios/{sid}").status_code == 200
        assert c2.get(f"/scenarios/{sid}/verification").status_code == 200
