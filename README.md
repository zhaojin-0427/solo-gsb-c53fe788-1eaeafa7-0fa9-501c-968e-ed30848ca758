# 配电保护选择性核验 API

基于 **Python + FastAPI + SQLite** 的配电保护选择性核验服务。客户端保存**不可变场景**
（单电源有向树 + 断路器动作曲线 + 故障集合），服务对每个故障核验主保护与各上级
断路器的时间选择性，并给出不合格电流范围、最小时间比等结构化结论。

## 功能与规则

- **不可变场景**：场景仅可创建与读取（无更新/删除接口），整体存入 SQLite。
- **拓扑校验**：必须是单电源有向树——电源节点无入边、其余节点入度恰为 1、
  全部节点可沿支路方向从电源到达；自环、环、多重入度、森林等均被拒绝（HTTP 422）。
- **支路与断路器**：每条支路至多装一台断路器（`breaker` 可缺省）。
- **曲线**：至少两个折点；折点电流为正且**严格递增**，动作时间为正。
  电流与时间均取对数后**线性插值**（log-log 折线），**禁止外推**。
- **故障**：指定支路、正电流闭区间 `[current_min, current_max]` 及门槛 `R > 1`。
- **主保护**：故障支路到电源路径上**最近**的断路器。
  - 路径上没有任何断路器 → 该故障结论为 **无保护**（`unprotected`）；
  - 有主保护而无上级 → **合格**（`qualified`）；
  - 否则对每台上级核验整个区间内的 `t上/t主 ≥ R`（**等号算合格**），
    任一上级不合格则该故障 **不合格**（`unqualified`）。
- **曲线覆盖**：故障区间必须被路径上每台断路器的曲线电流范围完整覆盖，
  否则保存场景时即被拒绝（HTTP 422，禁止外推）。
- **分段求值**：以两曲线折点（与区间端点）的并集分段。取对数后比值关于 `ln I`
  分段线性，故每段最值必在端点、段内至多一次过零，可精确求出：
  - 所有**不合格电流范围**及**边界是否包含**
    （交点处 `t上/t主 = R` 为合格，故交点边界不包含；
    区间端点若本身不合格则包含）；
  - **最小时间比**及对应电流（并列时取**较小电流**）。
- 浮点容差：`|ln(t上/t主) − ln R| ≤ 1e-9` 视为相等（等号合格）；
  最小比值并列判定相对容差 `1e-9`。

### 倍率配合方案（POST /scenarios/{id}/coordination-plan）

在**不修改已保存场景**的前提下，为所选断路器求一组动作时间**倍率** `k`
（新动作时间 = `k · 原动作时间`，仅作用于该断路器；共用同一曲线的其他断路器
不受影响，未指定的断路器倍率恒为 1），使**全部故障**满足选择性。

- **请求**：非空列表，每项给出断路器 id 与有限正数倍率闭区间
  `[k_min, k_max]`（允许相等）；未知 id、重复 id、倒置区间、空故障集均返回
  **422**。
- **约束**：每个故障必须有主保护。对每对主保护 `m` 与上级 `u`，沿用折点
  分段求得的**原始最小比值** `m`（倍率全为 1 时的 `min(t上/t主)`），联立::

      m · k上 / k主 ≥ R  （即 ln k主 − ln k上 ≤ ln m − ln R，等号合格）

  连同每台所选断路器的边界 `ln k_min ≤ ln k ≤ ln k_max`（未选者 `ln k = 0`），
  构成**差分约束系统**（可行 ⇔ 约束图无负环，Bellman–Ford 判定）。
- **优化目标**（两级，均有唯一值）：
  1. 先最小化所选断路器的 `max |ln k|`（调整幅度最小）；对
     `z = max|ln k|` 在可行区间上二分；
  2. 再在该最优可行域内按断路器 **id 字典序逐维最小化 `ln k`**
     （差分约束下每维最小可行值取“节点到常量节点最短路”的负值，逐维固定）。
- **无解（422，不返回方案）时分三类报告**：
  - **无保护故障**：故障路径上无任何断路器；
  - **未调整配对违规**：某主保护与上级**均未指定**倍率（恒为 1），原始最小
    比值已低于 R，任何方案都无法修复；
  - **约束冲突**：排除上述两类后，倍率边界与配对约束联立仍无解（例如两边界
    都钉死为 1 而原核验不合格）。三类原因可同时出现、一并列出。
- **响应**：按 id 字典序返回每台所选断路器的 `k`、`ln_k` 及区间，两个目标值
  （`max_abs_ln_k` 与字典序 `ln k` 向量），以及**应用倍率后逐故障重新核验**
  的完整结果。原场景文档与原核验接口结果均不改变。

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，SQLite 数据保存在命名卷 `selectivity-data`
（容器内 `/data/selectivity.db`），重启不丢失。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件默认 `./data/selectivity.db`，可用环境变量 `DATABASE_PATH` 覆盖。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 访问方式

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务信息与端点索引 |
| GET | `/health` | 健康检查 |
| POST | `/scenarios` | 保存不可变场景（校验拓扑/曲线/故障覆盖），201 返回 |
| GET | `/scenarios` | 场景列表（摘要） |
| GET | `/scenarios/{id}` | 读取场景原文 |
| GET | `/scenarios/{id}/verification` | 核验场景内全部故障 |
| GET | `/scenarios/{id}/faults/{fault_id}/verification` | 核验单个故障 |
| POST | `/scenarios/{id}/coordination-plan` | 在给定倍率闭区间内求配合方案（不改原场景，返回调整后核验） |

交互式文档（Swagger UI）：<http://localhost:8000/docs>（OpenAPI：`/openapi.json`）。

错误约定：参数/领域校验失败返回 **422** 且 `detail` 为错误信息数组；
场景或故障不存在返回 **404**；场景不可变，`PUT/PATCH/DELETE` 返回 **405**。

## 数据模型

```jsonc
{
  "name": "demo-feeder",
  "source": "S",                       // 电源节点
  "branches": [                        // 有向支路（from -> to）
    {"id": "B1", "from": "S", "to": "A",
     "breaker": {"id": "CB1", "curve_id": "C_UP"}},   // 至多一台断路器，可缺省
    {"id": "B3", "from": "B", "to": "C"}              // 无断路器
  ],
  "curves": [                          // 动作曲线：>=2 折点，电流正且严格递增，时间正
    {"id": "C_UP", "points": [{"current": 10, "time": 20},
                              {"current": 100, "time": 1.5}]}
  ],
  "faults": [                          // 故障：支路 + 正电流闭区间 + 门槛 R>1
    {"id": "F1", "branch_id": "B3", "current_min": 10, "current_max": 1000, "r": 1.8}
  ]
}
```

核验结果字段：

- `status`：`qualified`（合格）/ `unqualified`（不合格）/ `unprotected`（无保护）；
- `main_breaker_id`：主保护断路器（无保护时为 `null`）；
- `pairs[]`：主保护与每台上级的核验——
  - `min_ratio` / `min_ratio_current`：整个区间内的最小时间比及对应电流（并列取较小电流）；
  - `violations[]`：不合格电流范围，`from_included` / `to_included` 表示边界是否包含
    （边界本身是否不合格）。

## 实际请求与响应示例

以下示例为对本实现的真实调用与响应（场景 id 与时间为每次运行动态生成，
下文以 `<id>` 代指创建返回的 id）。示例场景文件见
[`examples/scenario.json`](examples/scenario.json)，拓扑为
`S -B1(CB1)-> A -B2(CB2)-> B -B3-> C`，另有 `S -B4-> D`（无断路器）。

### 1. 保存场景

```bash
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d @examples/scenario.json
```

响应 `201 Created`（节选，完整内容为场景原文 + `id` + `created_at`）：

```json
{
  "name": "demo-feeder",
  "source": "S",
  "branches": [
    {"id": "B1", "from": "S", "to": "A", "breaker": {"id": "CB1", "curve_id": "C_UP"}},
    {"id": "B2", "from": "A", "to": "B", "breaker": {"id": "CB2", "curve_id": "C_MAIN"}},
    {"id": "B3", "from": "B", "to": "C", "breaker": null},
    {"id": "B4", "from": "S", "to": "D", "breaker": null}
  ],
  "curves": [
    {"id": "C_MAIN", "points": [{"current": 10.0, "time": 10.0},
                                {"current": 100.0, "time": 1.0},
                                {"current": 1000.0, "time": 0.1}]},
    {"id": "C_UP", "points": [{"current": 10.0, "time": 20.0},
                              {"current": 100.0, "time": 1.5},
                              {"current": 1000.0, "time": 0.12}]}
  ],
  "faults": ["... 5 个故障，见 examples/scenario.json ..."],
  "id": "04853c31e7e444848ef9535c7b54193b",
  "created_at": "2026-09-29T20:06:45.863799+00:00"
}
```

### 2. 场景列表

```bash
curl -s http://localhost:8000/scenarios
```

```json
[
  {
    "id": "04853c31e7e444848ef9535c7b54193b",
    "name": "demo-feeder",
    "created_at": "2026-09-29T20:06:45.863799+00:00",
    "branch_count": 4,
    "curve_count": 2,
    "fault_count": 5
  }
]
```

### 3. 核验全部故障

```bash
curl -s http://localhost:8000/scenarios/<id>/verification
```

```json
{
  "scenario_id": "04853c31e7e444848ef9535c7b54193b",
  "scenario_name": "demo-feeder",
  "summary": {"total": 5, "qualified": 3, "unqualified": 1, "unprotected": 1},
  "faults": [
    {
      "fault_id": "F1",
      "branch_id": "B3",
      "status": "unqualified",
      "main_breaker_id": "CB2",
      "pairs": [
        {
          "upstream_breaker_id": "CB1",
          "min_ratio": 1.1999999999999997,
          "min_ratio_current": 1000.0,
          "qualified": false,
          "violations": [
            {"current_from": 23.240176447111455, "current_to": 1000.0,
             "from_included": false, "to_included": true}
          ]
        }
      ]
    },
    {
      "fault_id": "F2", "branch_id": "B3", "status": "qualified",
      "main_breaker_id": "CB2",
      "pairs": [
        {"upstream_breaker_id": "CB1", "min_ratio": 1.1999999999999997,
         "min_ratio_current": 1000.0, "qualified": true, "violations": []}
      ]
    },
    {
      "fault_id": "F3", "branch_id": "B2", "status": "qualified",
      "main_breaker_id": "CB2",
      "pairs": [
        {"upstream_breaker_id": "CB1", "min_ratio": 1.5,
         "min_ratio_current": 100.0, "qualified": true, "violations": []}
      ]
    },
    {
      "fault_id": "F4", "branch_id": "B1", "status": "qualified",
      "main_breaker_id": "CB1", "pairs": []
    },
    {
      "fault_id": "F5", "branch_id": "B4", "status": "unprotected",
      "main_breaker_id": null, "pairs": []
    }
  ]
}
```

结果解读（与示例曲线一致）：

- **F1**（区间 [10, 1000]，R=1.8）：`t上/t主` 在 10A 处为 2.0、100A 处为 1.5、
  1000A 处为 1.2。比值在 I≈23.24A 处穿过 1.8，故不合格范围为
  **(23.24, 1000]**——下边界为交点（等号合格，`from_included=false`），
  上边界本身比值 1.2 < 1.8（`to_included=true`）。最小时间比 1.2 @ 1000A。
- **F2**（R=1.1）：最小比值 1.2 ≥ 1.1，处处合格。
- **F3**（区间 [10, 100]，R=1.5）：右端点比值恰为 1.5，**等号合格**。
- **F4**：主保护 CB1 之上无上级，直接合格。
- **F5**：路径上无任何断路器，无保护。

### 4. 核验单个故障

```bash
curl -s http://localhost:8000/scenarios/<id>/faults/F1/verification
```

```json
{
  "fault_id": "F1",
  "branch_id": "B3",
  "status": "unqualified",
  "main_breaker_id": "CB2",
  "pairs": [
    {
      "upstream_breaker_id": "CB1",
      "min_ratio": 1.1999999999999997,
      "min_ratio_current": 1000.0,
      "qualified": false,
      "violations": [
        {"current_from": 23.240176447111455, "current_to": 1000.0,
         "from_included": false, "to_included": true}
      ]
    }
  ]
}
```

### 5. 求倍率配合方案

先保存一个仅含有保护故障的场景
[`examples/coordination-scenario.json`](examples/coordination-scenario.json)
（曲线与标准示例相同，故障 F1/F2/F3 路径上均有断路器），再提交配合请求
[`examples/coordination-plan.json`](examples/coordination-plan.json)，
为 CB1、CB2 各给出倍率闭区间 `[0.5, 2]`：

```bash
# 1) 保存场景
SID=$(curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-scenario.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 2) 求配合方案
curl -s -X POST http://localhost:8000/scenarios/$SID/coordination-plan \
  -H 'Content-Type: application/json' \
  -d @examples/coordination-plan.json
```

请求体：

```json
{
  "breakers": [
    {"id": "CB1", "k_min": 0.5, "k_max": 2.0},
    {"id": "CB2", "k_min": 0.5, "k_max": 2.0}
  ]
}
```

该场景 F1 的原始最小比值为 `m = 1.2 @1000A < R = 1.8`。约束
`1.2 · k_CB1 / k_CB2 ≥ 1.8` 即 `ln k_CB2 − ln k_CB1 ≤ ln(2/3)`。两级优化得到
`z* = ½ ln 1.5 ≈ 0.20273`，故 `k_CB1 = √1.5 ≈ 1.2247`、
`k_CB2 = √(2/3) ≈ 0.8165`（调整后 F1 最小比值恰为 1.8，**等号合格**）：

```json
{
  "scenario_id": "3fbc8553313447c2b0886f6c3aaa6fd7",
  "scenario_name": "coord-demo",
  "feasible": true,
  "multipliers": [
    {"id": "CB1", "k": 1.224744871269176, "ln_k": 0.2027325539541323,
     "k_min": 0.5, "k_max": 2.0},
    {"id": "CB2", "k": 0.8164965808461171, "ln_k": -0.20273255415403235,
     "k_min": 0.5, "k_max": 2.0}
  ],
  "objective": {
    "max_abs_ln_k": 0.20273255415403235,
    "lexicographic_ln_k": [0.2027325539541323, -0.20273255415403235]
  },
  "verification": {
    "scenario_id": "3fbc8553313447c2b0886f6c3aaa6fd7",
    "scenario_name": "coord-demo",
    "summary": {"total": 3, "qualified": 3, "unqualified": 0, "unprotected": 0},
    "faults": [
      {
        "fault_id": "F1", "branch_id": "B3", "status": "qualified",
        "main_breaker_id": "CB2",
        "pairs": [
          {"upstream_breaker_id": "CB1", "min_ratio": 1.8000000000000005,
           "min_ratio_current": 1000.0, "qualified": true, "violations": []}
        ]
      },
      {
        "fault_id": "F2", "branch_id": "B3", "status": "qualified",
        "main_breaker_id": "CB2",
        "pairs": [
          {"upstream_breaker_id": "CB1", "min_ratio": 1.8000000000000005,
           "min_ratio_current": 1000.0, "qualified": true, "violations": []}
        ]
      },
      {
        "fault_id": "F3", "branch_id": "B2", "status": "qualified",
        "main_breaker_id": "CB2",
        "pairs": [
          {"upstream_breaker_id": "CB1", "min_ratio": 2.2500000000000004,
           "min_ratio_current": 100.0, "qualified": true, "violations": []}
        ]
      }
    ]
  }
}
```

> 说明：上面的场景 `coord-demo` 与标准示例的曲线相同，故障为 F1（R=1.8）、
> F2（R=1.1）、F3（R=1.5）三个有保护故障。倍率只乘各自断路器：CB1 放慢
> 22.5%、CB2 加快至 81.6%；响应中的核验是按调整后动作时间**重新逐故障计算**
> 的，原场景与 `GET .../verification` 的结果保持不变。若把两台倍率都钉死为
> `[1, 1]`，约束无法满足，返回 **422**：
>
> ```json
> {"detail": ["倍率边界与全部配对选择性约束 m·k上/k主 ≥ R 无解（约束冲突），不返回方案"]}
> ```
>
> 其他 422 情形，例如含无保护故障时同时报告多类原因：
>
> ```json
> {
>   "detail": [
>     "故障 'F5' 无主保护（故障支路到电源路径上无断路器），无法配合",
>     "倍率边界与全部配对选择性约束 m·k上/k主 ≥ R 无解（约束冲突），不返回方案"
>   ]
> }
> ```
>
> 三台断路器串级（CB1→CB2→CB3）时，故障在 CB3 下，若违规配对 (CB3, CB1)
> 的两台断路器**都未指定倍率**（只选了无关的 CB2），报“未调整配对违规”：
>
> ```json
> {
>   "detail": [
>     "故障 'FX' 的主保护 'CB3' 与上级 'CB1' 均未指定倍率（恒为 1），原始最小比值 m=1.5 < R=1.8，未调整配对违规"
>   ]
> }
> ```
>
> 未知 id、倒置区间、空列表等参数错误同样为 **422**：
>
> ```json
> {"detail": ["断路器 id 未知: 'CB9'（场景中不存在该断路器）"]}
> ```

### 6. 错误示例：非树拓扑（HTTP 422）

```bash
curl -s -X POST http://localhost:8000/scenarios \
  -H 'Content-Type: application/json' \
  -d '{"name":"bad","source":"S",
       "branches":[{"id":"B1","from":"S","to":"A"},
                   {"id":"B2","from":"A","to":"B"},
                   {"id":"B3","from":"B","to":"A"}],
       "curves":[],"faults":[]}'
```

```json
{"detail": ["节点 'A' 的入度为 2（树中应为 1），拓扑不是树"]}
```

### 7. 错误示例：路径上曲线覆盖不足（HTTP 422）

故障区间 `[5, 500]` 超出曲线电流范围 `[10, 100]`（禁止外推）：

```json
{
  "detail": [
    "故障 'F9' 的电流区间 [5.0, 500.0] 超出路径上断路器 'CB2' 的曲线 'C2' 电流范围 [10.0, 100.0]（禁止外推，覆盖不足）",
    "故障 'F9' 的电流区间 [5.0, 500.0] 超出路径上断路器 'CB1' 的曲线 'C1' 电流范围 [10.0, 100.0]（禁止外推，覆盖不足）"
  ]
}
```

## 项目结构

```
├── app/
│   ├── main.py          # FastAPI 入口与路由
│   ├── core.py          # 领域校验 + 选择性分析引擎（log-log 分段求值）
│   ├── coordination.py  # 倍率配合：差分约束（Bellman-Ford）+ 两级优化 + 调整后核验
│   ├── schemas.py       # Pydantic 请求/响应模型
│   └── db.py            # SQLite 场景仓库（不可变文档）
├── tests/test_api.py    # 端到端与算法单元测试（38 例）
├── examples/scenario.json
├── examples/coordination-plan.json
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
