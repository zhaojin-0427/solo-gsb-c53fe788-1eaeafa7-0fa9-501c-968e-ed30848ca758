"""请求/响应模型（Pydantic v2）。

场景一旦保存即不可变：仅提供创建与读取接口，无更新/删除接口。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# 场景输入模型
# ---------------------------------------------------------------------------

class CurvePoint(BaseModel):
    """曲线折点：电流为正，动作时间为正。"""

    current: float = Field(gt=0, description="电流（安培），必须为正")
    time: float = Field(gt=0, description="动作时间（秒），必须为正")


class CurveIn(BaseModel):
    """动作特性曲线：至少两个折点，电流严格递增。"""

    id: str = Field(min_length=1)
    points: list[CurvePoint] = Field(min_length=2)

    @model_validator(mode="after")
    def _currents_strictly_increasing(self) -> "CurveIn":
        for prev, nxt in zip(self.points, self.points[1:]):
            if not nxt.current > prev.current:
                raise ValueError(
                    f"曲线 {self.id} 的折点电流必须严格递增: "
                    f"{prev.current} 之后出现 {nxt.current}"
                )
        return self


class BreakerIn(BaseModel):
    """断路器：每条支路至多一台（可缺省）。"""

    id: str = Field(min_length=1)
    curve_id: str = Field(min_length=1, description="引用的曲线 id")


class BranchIn(BaseModel):
    """有向支路：from -> to。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1)
    from_node: str = Field(min_length=1, alias="from")
    to_node: str = Field(min_length=1, alias="to")
    breaker: BreakerIn | None = None


class FaultIn(BaseModel):
    """故障：指定支路、正电流闭区间及门槛 R>1。"""

    id: str = Field(min_length=1)
    branch_id: str = Field(min_length=1)
    current_min: float = Field(gt=0)
    current_max: float = Field(gt=0)
    r: float = Field(gt=1, description="选择性门槛，必须大于 1")

    @model_validator(mode="after")
    def _interval_valid(self) -> "FaultIn":
        if self.current_min > self.current_max:
            raise ValueError(
                f"故障 {self.id} 的电流区间下界 {self.current_min} "
                f"大于上界 {self.current_max}"
            )
        return self


class ScenarioIn(BaseModel):
    """不可变场景：单电源有向树 + 曲线库 + 故障集合。"""

    name: str = Field(min_length=1)
    source: str = Field(min_length=1, description="电源节点名")
    branches: list[BranchIn] = Field(default_factory=list)
    curves: list[CurveIn] = Field(default_factory=list)
    faults: list[FaultIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 场景输出模型
# ---------------------------------------------------------------------------

class ScenarioOut(ScenarioIn):
    id: str
    created_at: str


class ScenarioSummary(BaseModel):
    id: str
    name: str
    created_at: str
    branch_count: int
    curve_count: int
    fault_count: int


# ---------------------------------------------------------------------------
# 核验结果模型
# ---------------------------------------------------------------------------

class ViolationOut(BaseModel):
    """一段不合格电流范围及边界是否包含。"""

    current_from: float
    current_to: float
    from_included: bool = Field(description="下边界本身是否不合格")
    to_included: bool = Field(description="上边界本身是否不合格")


class PairResultOut(BaseModel):
    """主保护与一台上级断路器的核验结果。"""

    upstream_breaker_id: str
    min_ratio: float = Field(description="整个故障区间内的最小时间比 t上/t主")
    min_ratio_current: float = Field(description="取得最小时间比的电流（并列取较小电流）")
    qualified: bool
    violations: list[ViolationOut] = Field(default_factory=list)


class FaultReportOut(BaseModel):
    fault_id: str
    branch_id: str
    status: str = Field(description="qualified=合格 / unqualified=不合格 / unprotected=无保护")
    main_breaker_id: str | None = Field(
        default=None, description="故障支路到电源路径上最近的断路器；无则为 null"
    )
    pairs: list[PairResultOut] = Field(default_factory=list)


class VerifySummaryOut(BaseModel):
    total: int
    qualified: int
    unqualified: int
    unprotected: int


class VerifyResponseOut(BaseModel):
    scenario_id: str
    scenario_name: str
    summary: VerifySummaryOut
    faults: list[FaultReportOut]
