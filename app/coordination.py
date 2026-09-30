"""倍率配合方案：在给定倍率闭区间内求解选择性配合。

模型
----
对每台被选择的断路器 j 给定倍率区间 [k_min_j, k_max_j]，倍率只乘该断路器的
动作时间（对数空间即整体平移 ln k_j）；未选择的断路器倍率恒为 1。令
x_j = ln k_j（未选择者 x_j = 0）。

对每个故障的每对（主保护 m，上级 u），记原始曲线（倍率全为 1）在整个故障
电流区间上的最小时间比为 m_ratio（沿用折点分段求得的原始最小比值），则
倍率后全区间合格当且仅当::

    m_ratio * k_u / k_m >= R
    => x_m - x_u <= ln m_ratio - ln R            （差约束）

连同倍率边界 ln k_min <= x_j <= ln k_max（未选择者 x_j = 0），构成差分约束
系统，可行 ⇔ 约束图无负环（Bellman-Ford 判定）。

优化
----
1. 主目标：最小化 z = max_j |x_j|。加入 |x_j| <= z 后对 z 二分（z=0 可行即
   无需调整）；
2. 次目标：在 z 最优的可行域内，按断路器 id 字典序逐维最小化 x_j（差分约束
   下 x_j 的最小可行值等于约束图中 j -> 0 的最短路长度取负）。

有解时返回倍率、目标值与倍率后逐故障核验；无解时区分：
- 无保护故障（unprotected）；
- 未调整配对违规（主保护与上级均未指定倍率而原始比值已低于 R）；
- 约束冲突（差分约束系统无解）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .core import (
    LOG_EPS,
    Curve,
    DomainError,
    _path_breakers,
    analyze_pair,
)
from .schemas import (
    CoordinationObjectiveOut,
    CoordinationPlanIn,
    CoordinationPlanOut,
    FaultIn,
    FaultReportOut,
    MultiplierOut,
    ScenarioIn,
    VerifyResponseOut,
    VerifySummaryOut,
)

# 二分 z 的迭代次数（~60 次即可达到 1e-14 以下相对精度）。
_Z_BISECTION_STEPS = 80
# Bellman-Ford 松弛的数值零容差（仅用于判定是否仍有更新）。
_BF_EPS = 1e-13
# 二分所得 z* 为“可行上确界”，取用时加该绝对余量，确保返回的方案严格
# 落在可行域内（其量级远小于核验容差 1e-9，对目标值与倍率无可观测影响）。
_Z_PAD = 1e-10


# ---------------------------------------------------------------------------
# 差分约束系统
# ---------------------------------------------------------------------------

@dataclass
class _Edge:
    """边 i -> j，权 w，表示 x_j - x_i <= w。节点 0 为常量节点 x_0 = 0。"""

    i: int
    j: int
    w: float


class _ConstraintGraph:
    """差分约束图：判定可行性、按字典序逐维求最小可行值。"""

    def __init__(self, n: int):
        self.n = n  # 变量节点数（不含常量节点 0）
        self.edges: list[_Edge] = []

    def bound_above(self, j: int, hi: float) -> None:
        # x_j <= hi
        self.edges.append(_Edge(0, j, hi))

    def bound_below(self, j: int, lo: float) -> None:
        # x_j >= lo  <=>  x_0 - x_j <= -lo
        self.edges.append(_Edge(j, 0, -lo))

    def fix(self, j: int, v: float) -> None:
        self.bound_above(j, v)
        self.bound_below(j, v)

    def pair(self, main_j: int, up_j: int, w: float) -> None:
        # x_main - x_up <= w
        self.edges.append(_Edge(up_j, main_j, w))

    def cap_abs(self, z: float, nodes: list[int] | None = None) -> list[_Edge]:
        """返回 |x_j| <= z 对应的附加边（不写入 self.edges）。"""
        if nodes is None:
            nodes = list(range(1, self.n + 1))
        extra: list[_Edge] = []
        for j in nodes:
            # x_j <= z 与 x_j >= -z（后者即 x_0 - x_j <= z），两条边权均为 z
            extra.append(_Edge(0, j, z))
            extra.append(_Edge(j, 0, z))
        return extra

    def feasible(self, extra: list[_Edge] | None = None) -> bool:
        """约束图（含附加边）是否无负环。

        以全部节点距离 0 初始化（等价于从虚拟超源走 0 权边到每个节点），
        Bellman-Ford 至多 V-1 轮收敛，第 V 轮仍可松弛即存在负环。
        """
        edges = self.edges if extra is None else self.edges + extra
        v_count = self.n + 1
        dist = [0.0] * v_count
        # 固定跑 V-1 轮后再检查：不使用“无更新即停”，因为按边表顺序松弛时，
        # 一条与扫描顺序相反的多跳链可能直到最后一轮才收敛。
        for _ in range(v_count - 1):
            for e in edges:
                cand = dist[e.i] + e.w
                if cand < dist[e.j] - _BF_EPS:
                    dist[e.j] = cand
        # 第 V 轮仍可松弛 => 存在负环
        for e in edges:
            if dist[e.i] + e.w < dist[e.j] - _BF_EPS:
                return False
        return True

    def min_values_prefix(self, order: list[int]) -> dict[int, float]:
        """按 order 逐维最小化 x_j，返回 {变量节点: 最小可行值}。

        已固定前缀下，x_j 的最小可行值 = -(j -> 0 的最短路)。在反图上从
        节点 0 跑 Bellman-Ford 即可一次求得所有 j -> 0 的最短路。
        """
        # 反图邻接：原边 i->j(w) 变为 j->i(w)
        rev: list[list[tuple[int, float]]] = [
            [] for _ in range(self.n + 1)
        ]
        for e in self.edges:
            rev[e.j].append((e.i, e.w))

        inf = math.inf

        def shortest_to_zero() -> list[float]:
            # 从节点 0（距离 0，其余 +inf）在反图上求最短路，即原图各
            # 节点到 0 的最短路；反图无负环（调用前已做可行性判定）。
            dist = [inf] * (self.n + 1)
            dist[0] = 0.0
            for _ in range(self.n):  # V - 1 轮（V = n + 1）
                for u in range(self.n + 1):
                    du = dist[u]
                    if du == inf:
                        continue
                    for v, w in rev[u]:
                        cand = du + w
                        if cand < dist[v] - _BF_EPS:
                            dist[v] = cand
            return dist

        fixed: dict[int, float] = {}
        for j in order:
            dist = shortest_to_zero()
            value = -dist[j]
            fixed[j] = value
            # 固定 x_j = value：原图加两条边，反图按“原边 (i,j,w) 记为
            # rev[j].append((i, w))”的同样规则加入。
            # 原 0 -> j (value)  => rev[j]: (0, value)
            # 原 j -> 0 (-value) => rev[0]: (j, -value)
            self.fix(j, value)
            rev[j].append((0, value))
            rev[0].append((j, -value))
        return fixed


# ---------------------------------------------------------------------------
# 方案求解
# ---------------------------------------------------------------------------

def build_coordination_plan(
    scenario_id: str,
    scenario: ScenarioIn,
    request: CoordinationPlanIn,
) -> CoordinationPlanOut:
    """求解倍率配合方案；无解抛 DomainError（HTTP 422），不改动原场景。"""
    if not scenario.faults:
        raise DomainError(["场景故障集为空，无配合对象，拒绝生成配合方案"])

    # --- 选择的断路器（id 已知、不重复）与倍率边界 ---
    breaker_branch = {
        b.breaker.id: b
        for b in scenario.branches if b.breaker is not None
    }
    selected: dict[str, tuple[float, float]] = {}
    unknown: list[str] = []
    for item in request.breakers:
        if item.id not in breaker_branch:
            unknown.append(
                f"断路器 id 未知: {item.id!r}（场景中不存在该断路器）"
            )
            continue
        selected[item.id] = (item.k_min, item.k_max)
    if unknown:
        raise DomainError(unknown)

    ordered_ids = sorted(selected)  # 次目标：id 字典序
    index = {bid: j + 1 for j, bid in enumerate(ordered_ids)}
    curves = {c.id: Curve.from_points(c.id, c.points) for c in scenario.curves}

    # --- 逐故障提取配对、原始最小比值 m，并分类无解原因 ---
    # pair_rows 每项: (fault, main_id, up_id, m_ratio, is_unadjusted_violation)
    pair_rows: list[tuple] = []
    unprotected_faults: list[str] = []
    unadjusted: list[str] = []

    for fault in scenario.faults:
        path = list(_path_breakers(scenario, fault.branch_id))
        if not path:
            unprotected_faults.append(fault.id)
            continue
        main_br = path[0]
        main_curve = curves[main_br.curve_id]
        for up_br in path[1:]:
            result = analyze_pair(
                main_curve,
                curves[up_br.curve_id],
                fault.current_min,
                fault.current_max,
                fault.r,
                up_br.id,
            )
            m_ratio = result.min_ratio
            # 未调整配对：主保护与上级都未指定倍率（恒为 1）且原始比值已低于 R
            pair_unadjusted = (
                main_br.id not in selected
                and up_br.id not in selected
                and math.log(m_ratio) - math.log(fault.r) < -LOG_EPS
            )
            pair_rows.append(
                (fault, main_br.id, up_br.id, m_ratio, pair_unadjusted)
            )
            if pair_unadjusted:
                unadjusted.append(
                    f"故障 {fault.id!r} 的主保护 {main_br.id!r} 与上级 "
                    f"{up_br.id!r} 均未指定倍率（恒为 1），原始最小比值 "
                    f"m={m_ratio:g} < R={fault.r:g}，未调整配对违规"
                )

    errors = [
        f"故障 {fid!r} 无主保护（故障支路到电源路径上无断路器），无法配合"
        for fid in unprotected_faults
    ]
    errors.extend(unadjusted)

    # --- 组装差分约束（配对约束 + 边界；未选变量 x=0） ---
    graph = _ConstraintGraph(len(ordered_ids))
    for bid in ordered_ids:
        j = index[bid]
        k_min, k_max = selected[bid]
        graph.bound_below(j, math.log(k_min))
        graph.bound_above(j, math.log(k_max))
    # 未选择的断路器作为“固定 0 变量”参与配对约束
    fixed_zero: dict[str, int] = {}

    def var_of(bid: str) -> int:
        if bid in index:
            return index[bid]
        if bid not in fixed_zero:
            # 扩展一个固定为 0 的临时变量节点
            new_j = graph.n + 1
            graph.n = new_j
            graph.fix(new_j, 0.0)
            fixed_zero[bid] = new_j
        return fixed_zero[bid]

    # 未调整违规配对本身恒不成立、且已单独报错，不计入“约束冲突”判定，
    # 使两类原因互斥、不重复报告。
    for _fault, main_id, up_id, m_ratio, pair_unadjusted in pair_rows:
        if pair_unadjusted:
            continue
        w = math.log(m_ratio) - math.log(_fault.r)
        graph.pair(var_of(main_id), var_of(up_id), w)

    # --- 边界+配对本身是否可行（无 |x|<=z 附加约束） ---
    if not graph.feasible():
        errors.append(
            "倍率边界与全部配对选择性约束 m·k上/k主 ≥ R 无解（约束冲突），"
            "不返回方案"
        )
    # 三类无解原因合并返回：无保护故障、未调整配对违规、约束冲突。
    if errors:
        raise DomainError(errors)

    # --- 主目标：二分最小 z = max|x_j|（仅对所选变量封顶） ---
    z_max = max(
        max(abs(math.log(lo)), abs(math.log(hi)))
        for lo, hi in selected.values()
    )
    if z_max == 0.0:
        z_star = 0.0
    else:
        lo_z, hi_z = 0.0, z_max
        capped_nodes = [index[bid] for bid in ordered_ids]
        for _ in range(_Z_BISECTION_STEPS):
            mid = 0.5 * (lo_z + hi_z)
            caps = graph.cap_abs(mid, capped_nodes)
            if graph.feasible(extra=caps):
                hi_z = mid
            else:
                lo_z = mid
        z_star = hi_z + _Z_PAD

    # --- 次目标：z* 可行域内按 id 字典序逐维最小化 x ---
    graph.edges.extend(
        graph.cap_abs(z_star, [index[bid] for bid in ordered_ids])
    )
    values = graph.min_values_prefix([index[bid] for bid in ordered_ids])

    # 数值漂移夹回边界与 |x| <= z*，并规范化 -0.0
    x_values: dict[str, float] = {}
    for bid in ordered_ids:
        j = index[bid]
        lo = math.log(selected[bid][0])
        hi = math.log(selected[bid][1])
        v = min(hi, max(lo, values[j]))
        v = min(z_star, max(-z_star, v))
        x_values[bid] = v + 0.0

    multipliers = [
        MultiplierOut(
            id=bid,
            k=math.exp(x_values[bid]),
            ln_k=x_values[bid],
            k_min=selected[bid][0],
            k_max=selected[bid][1],
        )
        for bid in ordered_ids
    ]

    # --- 应用倍率后逐故障重新核验（不改动原场景） ---
    # 倍率 k=exp(x) 对动作时间的乘法在对数空间即平移 x；未指定者平移 0。
    x_by_breaker = {bid: x_values[bid] for bid in ordered_ids}
    scaled_curves: dict[str, Curve] = {}
    for br in (b.breaker for b in scenario.branches if b.breaker is not None):
        base = curves[br.curve_id]
        shift = x_by_breaker.get(br.id, 0.0) + 0.0
        scaled_curves[br.id] = Curve(
            id=base.id,
            currents=base.currents,
            log_currents=base.log_currents,
            log_times=tuple(t + shift for t in base.log_times),
        )

    reports = [
        _evaluate_fault_scaled(scenario, fault, scaled_curves)
        for fault in scenario.faults
    ]
    summary = VerifySummaryOut(
        total=len(reports),
        qualified=sum(1 for r in reports if r.status == "qualified"),
        unqualified=sum(1 for r in reports if r.status == "unqualified"),
        unprotected=sum(1 for r in reports if r.status == "unprotected"),
    )
    verification = VerifyResponseOut(
        scenario_id=scenario_id,
        scenario_name=scenario.name,
        summary=summary,
        faults=reports,
    )

    return CoordinationPlanOut(
        scenario_id=scenario_id,
        scenario_name=scenario.name,
        multipliers=multipliers,
        objective=CoordinationObjectiveOut(
            max_abs_ln_k=max((abs(x) for x in x_values.values()), default=0.0),
            lexicographic_ln_k=[x_values[bid] for bid in ordered_ids],
        ),
        verification=verification,
    )


def _evaluate_fault_scaled(
    scenario: ScenarioIn,
    fault: FaultIn,
    scaled_curves: dict[str, Curve],
) -> FaultReportOut:
    """用每台断路器各自（按倍率平移后）的曲线核验单个故障。"""
    path = list(_path_breakers(scenario, fault.branch_id))
    if not path:
        return FaultReportOut(
            fault_id=fault.id,
            branch_id=fault.branch_id,
            status="unprotected",
            main_breaker_id=None,
            pairs=[],
        )
    main_breaker = path[0]
    pairs = [
        analyze_pair(
            scaled_curves[main_breaker.id],
            scaled_curves[up.id],
            fault.current_min,
            fault.current_max,
            fault.r,
            up.id,
        )
        for up in path[1:]
    ]
    status = "qualified" if all(p.qualified for p in pairs) else "unqualified"
    return FaultReportOut(
        fault_id=fault.id,
        branch_id=fault.branch_id,
        status=status,
        main_breaker_id=main_breaker.id,
        pairs=pairs,
    )
