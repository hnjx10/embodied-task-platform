# 架构

## 分层与依赖方向

```
 L6  交付层        cli.py │ console.py │ api/       ← 参数解析、协议转换、输出渲染
 L5  组合根        factory.py                       ← 唯一知道所有具体实现的地方
 L4  编排层        orchestration/                   ← 一次任务的生命周期
                     graph │ runner │ executor │ events │ maintenance
 L3  领域服务      planner/ │ safety/               ← 把能力编排成决策
 L2  能力层        skills/ │ storage/                ← 真正干活：驱动执行器、落库
 L1  领域模型      domain/ │ reporting.py            ← 纯数据契约 + 纯函数渲染
 L0  基础设施      config │ logging │ metrics │ audit │ errors │ version
```

**依赖只向下**。同层之间不互相 import：`planner` 不认识 `safety`，`skills` 不认识
`storage`，它们共同依赖的只有 `domain` 里的契约对象。

为什么 `domain/` 要单独成层：`PlanStep` 同时被规划、安全、编排、存储四方使用。
如果它长在 `planner/` 里，其它三层就得反向依赖规划层——这正是一个包变成
「什么都能干」的开始。抽成纯模型后，`safety` 只依赖「计划长什么样」，
不依赖「计划怎么生成」。

### 分层约束由测试强制

`tests/unit/test_layering.py` 用 AST 解析全部源码，在每次提交上校验六条规则
（不导入模块，所以无 ROS 环境下同样有效）：

| 规则 | 违反后果 |
|---|---|
| 每个包必须在 `LAYER_RANK` 登记层号 | 新增包不能悄悄绕过分层 |
| 依赖只能指向同层或更低层 | 防止 `skills` 反向 import `api` |
| `domain` 不得有任何对外依赖 | 防止契约层被基础设施污染 |
| 独立纵切不得互相依赖（planner↔safety、skills↔storage） | 防止职责边界模糊 |
| 只有交付层可以 import `factory` | 防止绕过依赖注入 |
| 包级依赖图无环 | 保证每一层可被单独理解与测试 |

违规时的报错直接给出行号级结论，例如
`robot_agent.skills (L2) 反向依赖 robot_agent.orchestration (L4)`。

`factory.py` 是唯一的装配点。CLI、HTTP 服务、测试都调用 `build_assembly()`，
保证「跑起来的东西」和「测的东西」是同一套对象图。

---

## 一次任务的完整路径

```
1. submit()      创建 TaskRecord（queued），幂等键去重，写审计 task_submitted
2. execute()     CAS queued → running，抢占成功者才继续
3. graph.invoke()（在线程池里跑，不阻塞事件循环）
   ├─ plan     Planner: LLM →(失败/超时/熔断) 规则兜底
   ├─ screen   SafetyGate: 逐步过策略链，任一拒绝 → 整条不执行
   │           └─ 需要审批 → 状态置 awaiting_approval，图直接结束
   ├─ execute  StepExecutor: 单步超时/重试，失败即停
   │           └─ 每步通过回调推事件到 EventBus
   ├─ verify   用 get_pose 实测位姿，不信执行器的自述
   └─ report   生成人类可读报告
4. 收尾       落库（succeeded/failed/blocked）、指标、审计、事件 end
5. 审批路径   approve() CAS awaiting_approval → running，跑 resume_graph
             （跳过 plan，但**重跑 screen**）
```

---

## 关键抽象

### Skill（能力）

```python
class Skill(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    params_model: ClassVar[type[SkillParams]]   # pydantic，用于校验 + 生成 LLM 描述
    risk: ClassVar[RiskLevel]                   # low/medium/high，安全层与审批用

    def execute(self, **kwargs) -> SkillResult:  # 唯一外部入口：校验+计时+异常归一
    @abstractmethod
    def run(self, **kwargs) -> SkillResult:      # 子类只实现这里
```

`execute` 是模板方法：子类无法绕过参数校验与指标埋点。

### Policy（安全约束）

```python
class Policy(Protocol):
    name: str
    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision
```

`PolicyDecision` 有三态语义：放行 / 放行但修正参数（降速）/ 拒绝。
`requires_approval=True` 是第四种：合法但必须人工确认。

新增约束 = 加一个类 + 在 `default_policies()` 里注册。主流程零改动。

### Planner（规划）

`Planner` 是门面，内部持有 `LLMPlanner`（可为 None）与 `RulePlanner`。
降级是显式的：降级时 `Plan.warnings` 留痕，指标 `planning_total{source="rules"}` 可统计。

### TaskRepository（存储）

`Protocol` + 两个实现（内存 / SQLite）。`transition(expected, new)` 是 CAS 语义，
这是并发审批不重复执行的唯一保证。

### EventBus（事件）

按 `task_id` 分组的广播。工作线程用 `call_soon_threadsafe` 投递回事件循环，
订阅队列满时丢最旧事件（背压保护生产者），终态投递 `None` 哨兵让 SSE 自然关闭。

---

## 并发模型

```
HTTP 请求 ──► TaskRunner
                ├─ asyncio.Semaphore(max_concurrent_tasks)  限制同时处理的任务数
                ├─ asyncio.Semaphore(1)                     物理动作串行
                └─ asyncio.to_thread(graph.invoke)          同步图跑在线程里
                        └─ StepExecutor: ThreadPoolExecutor(max_workers=1)
```

三层限制各有原因：

1. **任务级**：限制等待中的任务数，避免内存与队列无限增长；
2. **动作级**：一台机器人同一时刻只能执行一个动作序列，这是物理约束；
3. **线程池**：技能调用是阻塞的（ROS 的 `while` 轮询），必须与事件循环隔离。

技能超时后线程仍在跑（Python 无法强杀），所以超时处理是「放弃等待 + 急停」，
并在 `OPERATIONS.md` 里记录了这个已知限制。

---

## 为什么用 LangGraph

也可以用 `for` 循环 + 状态机。选择 LangGraph 的三个具体理由：

1. **审批挂起/恢复天然**：`awaiting_approval` 是一个节点后的分支，不需要在业务代码里写
   「保存现场 → 退出 → 恢复现场」的胶水；
2. **状态显式**：`GraphState` 是 TypedDict，`outcomes` 用 `operator.add` 累加，
   新增阶段是加节点加边，而不是在长函数里插 `if`；
3. **可替换 checkpointer**：当前用无 checkpointer 的两段式（完整图 + 续跑图），
   接 SqliteSaver 后可直接支持跨进程续跑，业务代码不变。

代价是多一个依赖与一层间接。对于需要人工介入的物理系统，这个代价值得。

---

## 扩展点

| 想做的事 | 改哪里 | 是否需要动主流程 |
|---|---|---|
| 新增机器人技能 | 新建 `Skill` 子类并注册 | 否 |
| 新增安全约束 | 新建 `Policy` 并加入链 | 否 |
| 换执行器（Isaac Sim / 真机） | 新建 `skills/xxx.py`，实现同名技能 | 否（改配置） |
| 换存储（PostgreSQL） | 实现 `TaskRepository` 协议 | 否（改配置） |
| 换 LLM 供应商 | 改 `llm.base_url` / `model` | 否 |
| 加第三方技能包 | `SkillRegistry.load_plugins()`（entry points） | 否 |
| 加新阶段（如重规划） | `graph.py` 加节点与边 | 是（编排层内） |

---

## 失败模式与隔离

| 故障 | 隔离方式 | 用户看到 |
|---|---|---|
| LLM 超时/挂了 | 熔断 + 降级规则 | 任务照常完成，`plan_source=rules` |
| LLM 输出幻觉技能 | 白名单策略拦截 | `blocked`，原因写明未知技能 |
| 技能超时 | 放弃等待 + 急停 + 不重试 | `failed`，`outcome=timeout` |
| 技能瞬时异常 | 有限重试（仅 `retryable`） | 成功后无感；失败则 `failed` |
| 安全策略自身崩溃 | fail-closed 当拒绝 | `blocked`，原因含「策略内部错误」 |
| SQLite 锁冲突 | `busy_timeout=5000` + 写锁串行 | 短暂延迟 |
| 审计写失败 | 默认不阻塞（可配 fail_closed） | 服务继续，指标 +1，日志 error |
| 客户端断连（SSE） | 检测 `is_disconnected` 并退订 | 无泄漏 |
