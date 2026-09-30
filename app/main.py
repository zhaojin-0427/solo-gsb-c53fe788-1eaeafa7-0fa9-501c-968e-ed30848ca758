"""配电保护选择性核验 API（FastAPI 入口）。"""
from __future__ import annotations

import math

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .core import DomainError, validate_scenario, verify_scenario
from .coordination import build_coordination_plan
from .db import ScenarioStore
from .schemas import (
    CoordinationPlanIn,
    CoordinationPlanOut,
    FaultReportOut,
    ScenarioIn,
    ScenarioOut,
    ScenarioSummary,
    VerifyResponseOut,
)


def _sanitize_json(obj: object) -> object:
    """把 NaN/±Infinity 替换为字符串，保证 422 错误体是合法 JSON。

    Pydantic 的字段错误会回显原始输入；若客户端提交 NaN，默认错误处理器
    会因 json.dumps 拒绝非有限浮点而抛 500。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, BaseException):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return str(obj)


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="配电保护选择性核验 API",
        version="1.0.0",
        description=(
            "保存不可变的配电场景（单电源有向树 + 断路器动作曲线 + 故障集合），"
            "并对每个故障核验主保护与各上级断路器的时间选择性 "
            "（对数-对数插值，禁止外推，t上/t主 ≥ R，等号合格）。"
        ),
    )
    app.state.store = ScenarioStore(db_path)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": _sanitize_json(exc.errors())},
        )

    def _get_scenario_or_404(scenario_id: str) -> dict:
        record = app.state.store.get(scenario_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景不存在: {scenario_id}",
            )
        return record

    # ------------------------------------------------------------------
    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {
            "service": "配电保护选择性核验 API",
            "version": "1.0.0",
            "docs": "/docs",
            "endpoints": {
                "create_scenario": "POST /scenarios",
                "list_scenarios": "GET /scenarios",
                "get_scenario": "GET /scenarios/{id}",
                "verify_all": "GET /scenarios/{id}/verification",
                "verify_one": "GET /scenarios/{id}/faults/{fault_id}/verification",
                "coordination_plan": "POST /scenarios/{id}/coordination-plan",
            },
        }

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    @app.post(
        "/scenarios",
        response_model=ScenarioOut,
        status_code=status.HTTP_201_CREATED,
        tags=["scenarios"],
        summary="保存不可变场景（校验拓扑、曲线与故障覆盖）",
    )
    def create_scenario(payload: ScenarioIn) -> dict:
        validate_scenario(payload)
        return app.state.store.create(
            payload.model_dump(mode="json", by_alias=True)
        )

    @app.get(
        "/scenarios",
        response_model=list[ScenarioSummary],
        tags=["scenarios"],
        summary="列出全部场景",
    )
    def list_scenarios() -> list[dict]:
        return app.state.store.list()

    @app.get(
        "/scenarios/{scenario_id}",
        response_model=ScenarioOut,
        tags=["scenarios"],
        summary="读取场景（场景不可变，无更新/删除接口）",
    )
    def get_scenario(scenario_id: str) -> dict:
        return _get_scenario_or_404(scenario_id)

    # ------------------------------------------------------------------
    @app.get(
        "/scenarios/{scenario_id}/verification",
        response_model=VerifyResponseOut,
        tags=["verification"],
        summary="核验场景内全部故障的选择性",
    )
    def verify_all(scenario_id: str) -> VerifyResponseOut:
        record = _get_scenario_or_404(scenario_id)
        scenario = ScenarioIn.model_validate(record)
        return verify_scenario(scenario_id, scenario)

    @app.get(
        "/scenarios/{scenario_id}/faults/{fault_id}/verification",
        response_model=FaultReportOut,
        tags=["verification"],
        summary="核验单个故障的选择性",
    )
    def verify_one(scenario_id: str, fault_id: str) -> FaultReportOut:
        record = _get_scenario_or_404(scenario_id)
        scenario = ScenarioIn.model_validate(record)
        report = verify_scenario(scenario_id, scenario)
        for fault_report in report.faults:
            if fault_report.fault_id == fault_id:
                return fault_report
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"故障不存在: {fault_id}",
        )

    # ------------------------------------------------------------------
    @app.post(
        "/scenarios/{scenario_id}/coordination-plan",
        response_model=CoordinationPlanOut,
        tags=["coordination"],
        summary="在倍率闭区间内求解配合方案（不改原场景，返回调整后核验）",
    )
    def coordination_plan(
        scenario_id: str, payload: CoordinationPlanIn
    ) -> CoordinationPlanOut:
        record = _get_scenario_or_404(scenario_id)
        scenario = ScenarioIn.model_validate(record)
        return build_coordination_plan(scenario_id, scenario, payload)

    return app


app = create_app()
