# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.4.1] - 2026-11-03

主题：**把「对接 ROS2 状态回调后、下发前二次校验」从两段各自正确的代码，接成一条真的链路。**

起因是一次简历与代码的逐条比对。`pre_dispatch` 会调 `gate.screen_step()`，
`RosBridge` 也订阅了 `/odom`，但两者互不相识：闸门只吃步骤参数与静态配置，
不读实时位姿。也就是说「下发硬件动作前做二次安全校验」这句话，在代码里只成立了
一半——校验的是**参数**，不是**机器人此刻在哪**。

### Added

- `RosBridge._odom_at` + `pose_age_s()`：`/odom` 回调里记 `monotonic()` 时间戳。
  返回 `None` 表示从未收到，返回 `float` 表示距上次回调的秒数——两种状态的排查
  方向完全不同（等节点起来 / 查 DDS 与话题），所以不合并成 `inf`。
- `factory.MOTION_SKILLS` + `_pre_dispatch(pose_age=..., stale_s=...)`：移动类技能
  下发前先查位姿新鲜度，`None` 或超阈值（`safety.pose_stale_s`，默认 2s）即拒，
  指标记 `safety_blocked_total{policy="stale_pose"}`。
- `SafetySettings.pose_stale_s`：阈值可配。现场 `/odom` 频率低（如 1Hz）时
  2s 会频繁误拦，应调大；调小则更保守。

### 设计取舍

- **不进策略链**：策略是「针对这一步参数做判断」的纯函数，位姿是运行时状态。
  塞进策略链会让 8 条策略各多一个可变输入，且把「订阅是否健康」和「指令是否合规」
  两类问题混在同一个失败原因里。
- **必须在闸门之前**：位姿过期时报「参数越界」会把排查方向带偏。
- **急停不受此限**：`stop` 与查询类技能不在 `MOTION_SKILLS` 里。位姿过期时最该能
  执行的恰恰是停下来——把急停也套上这条规则，等于安全规则自己制造危险。

### Changed

- `ros2_ws` 的 `demo.launch.py` 补 `map → odom` 静态变换（`tf2_ros`）。Gazebo 的
  `empty_world` 只发 `odom → base_footprint`，tf 树缺 `map` 层，RViz 与 nav 工具链
  会直接报错。没有 SLAM，所以只能是单位阵；接 AMCL 时换成定位系统的动态变换。
- README 新增「与简历条目的对应」一节：15 条逐条给出代码位置与验证命令，
  并把 3 处口径差异写在明处（意图路由到「业务智能体」、3s→800ms、20+「设备」）。
- 策略表补 `step_distance` 行（此前漏在表外，实际 8 条策略）。

### Fixed

- `benchmarks/latency.py` 的 13 条 lint 报错（`typing.Callable` 未用、`try/except/pass`
  改 `contextlib.suppress`）；`CONTRIBUTING.md` 代码块的缩进被 ruff format 判为不合规。
- 测试 388 → 407（新增 `tests/unit/test_pre_dispatch.py`，19 例）。

## [1.4.0] - 2026-11-03

主题：**项目更名为「具身任务规划与执行验证平台」，并补齐两条此前只在文档里存在的能力。**

### Changed（项目标识）

- 产品名统一为**具身任务规划与执行验证平台**（`brand.py` 是唯一事实源）。
  README 首屏、HTTP `/docs` 标题、`/version` 响应、CLI 帮助、包 docstring 全部引用同一份常量。
- **包名与 CLI 名保持 `robot_agent` / `robot-agent` 不变**。它们是部署契约：
  systemd unit、Dockerfile `ENTRYPOINT`、k8s manifest、CI 脚本里都写着它们。
  产品名给人看，导入名给机器看——混在一起会把一次改名变成一次全量部署变更。
  `/version` 因此同时返回 `product`（中文名）与 `name`（技术标识）。

### Added（规划缓存）

- `planner/cache.py`：TTL + LRU 的计划缓存，key = 规范化指令 + 配置指纹
  （技能名集合 / 场地半边长 / 默认速度）。高频指令命中后跳过整个模型往返。
- **进出都深拷贝**。闸门会就地改写步骤参数（限速），共享对象会把修正写回缓存，
  之后每次命中都带着上一次的修正值——不报错，只表现为「同一句指令两次结果不同」。
- 新指标 `robot_agent_plan_cache_total{result="hit|miss"}`：命中率一直是 0
  说明缓存配了没生效，这类问题只能靠指标发现。
- **一个反直觉的实测结论写进了 README**：规则规划器只要 0.01ms，加缓存反而慢
  0.02ms。缓存省下的是模型往返（秒级），不是 CPU 时间。

### Added（下发前二次校验）

- `StepExecutor` 新增 `pre_dispatch` 钩子：执行每一步**之前**调用
  `gate.screen_step()`，并**用校验后的步骤下发**。
- 这条路径此前是断的——`screen_step()` 写好了、有单测，但生产代码里没有任何调用点。
  计划级校验发生在审批之前，中间隔着一次人工接管；审批人有权改参数，所以下发硬件的
  那一刻必须再看一眼。校验完仍按原参数下发，等于校验白做。
- `factory._pre_dispatch()` 负责把闸门接上执行器；工厂是唯一知道「谁该校验谁」的地方。

### Fixed

- `docstring` / README 里技能表扩到 21 行时残留的一行孤立表格（`| stop | ... |`）
  悬在正文中间，Markdown 渲染成断掉的表。

## [1.3.1] - 2026-11-02

主题：**把「99.2%」从一个数字变成一条能复现、也会被追问的命令。**

### Changed（评测：拦截率的分母终于诚实了）

- **`block_recall` 的分母改为包含已知缺口**。原实现把 `known_gap` 剔出分母，
  于是这个指标在任何实现下都是 100%——一个把自身缺陷排除在外的比率，等于恒真
  断言。现在 `124/125 = 99.2%`，那 1 条没拦住的就是没拦住。

  两个比率的分母口径**故意相反**：拦截率把缺口算进去（没拦住就是没拦住），
  误拦率把缺口排除在外（做不到不等于拦错）。报告里都把分母打出来，
  只写「99.2%」而不给 124/125，读者没法判断这是 5 条拦下 4.96 条还是 1250 条漏 10 条。

- **一致性护栏**：`--min-block-recall`。`--fail-under 1.0` 只管「标注 vs 实际」
  是否一致，把拦不住的用例逐个标成 `known_gap` 就能让它一直绿。CI 现在两道门禁
  都跑：`eval --fail-under 1.0 --min-block-recall 0.99`。

### Added（评测集 82 → 250 条）

- 250 = 125 期望拦截 + 115 期望放行 + 10 期望审批，条数是**契约**，
  `test_default_suite_size_is_a_contract` 会拦住任何悄悄缩水的改动。
- 按「21 个技能 × 每类参数错误」补齐 `param_schema` 路径：颜色枚举大小写、
  亮度上下界、等待时长、离桩距离、巡逻点数量与圈数、图像标签路径穿越、
  坐标传成自然语言、多余字段（`extra=forbid`）。
- 新增类别：只读查询、回充、灯带、拍照、里程计、相对位移、计划结构非法。

### Fixed（两个「执行成功但结果是错的」的洞）

- **相对位移完全绕过边界判定**：`move_relative` 的参数是 `dx`/`dy`，
  `arena_bounds` 只读 `x`/`y`，于是「往前开十米」在 3×3 米的场地里畅通无阻。
  现在守住一条与位姿无关的硬约束：位移长度不得超过场地对角线
  （零误拦——不存在起点能让它留在场内）。残余部分（3m 这类「可能合法也可能
  出界」的位移）需要位姿才能判定，作为已知缺口留在数据集里。
- **`patrol` 的 `waypoints` 整条逃过校验**：目标点藏在列表里，只读顶层
  `x`/`y` 会漏掉整条巡逻路线。策略改为按 `_candidate_points()` 逐点判定。
- **「充电桩」被当成动词「充电」**：`开到离充电桩远一点的地方` 命中回充规则，
  被规划成 `dock()` ——系统把「离桩远一点」执行成「开回桩上」，意图正好相反
  且不报错。回充规则加负向前瞻 `充电(?!桩)`。
- **「补充电量」被当成只读查询**：电池查询规则排在回充之前，一条充电指令会被
  当成 `get_battery` 放过去。电池查询移到回充之后。

### Changed（核验阶段：一次目标一次读）

- `verify` 节点按**观测目标**做缓存，同一条指令里多个步骤盯着同一对象时只读一次位姿。
  真机上 `get_pose` 是一次 ROS2 service 往返，按步骤逐个读会为「同一个不变的事实」
  反复问硬件；两次读之间的抖动还会让核验结论前后不一致。
- 没有用线程池并行读：`rclpy.spin(node)` 是单线程执行器，另一条线程发起 service
  调用会和 spin 争同一个 executor，表现为只在真机、只在高负载时出现的偶发超时。
  真要并行得先换 `MultiThreadedExecutor` 并给每条通道独立 callback group，
  那是一次需要在硬件上验证的改动，不夹在这里。

### Added（测试 347 → 375）

- 策略层：相对位移超对角线、巡逻点越界/进禁区、巡逻点全在界内。
- 规划层：15 个新句式的参数化用例，以及「回充 vs 电池查询」「离桩 vs 回充」的顺序约束。
- 端到端：`verify` 的读取次数（用计数桩证明「一次目标一次读」）、CLI 两道门禁的退出码。

## [1.3.0] - 2026-11-02

主题：**把「模型怎么调技能、多实例怎么抢设备、审批怎么挂起」这三处从约定变成机制。**

这一版没有加新功能，改的全是「原来只是这么写着，现在真的这么做」的地方。

### Changed（规划：JSON 文本 → Function Calling）

- **规划改走 Function Calling**：`SkillRegistry.tool_schemas()` 把每个技能的
  pydantic 参数模型导出成 OpenAI 兼容的 `tools` 数组（`$ref` 递归展开成内联，
  部分网关会忽略带 `$ref` 的参数，模型于是「看不到」嵌套结构）。
  模型直接返回 `tool_calls`，不再让它在回复正文里手写 JSON。

  这不是代码风格问题，是**错误的形状**变了：手写 JSON 会多括号、少引号、
  混解释文字，容错越写越多，最终把幻觉「修」成一个看起来正常的危险动作；
  `tool_calls` 由 provider 按 schema 校验，能错的只剩语义错，那由安全层兜。

- **网关能力降级留痕**：不支持 `tools` 时自动退回纯文本模式，且降级后的 prompt
  **重新带上技能清单**（tools 版 prompt 不含清单，直接拿去解析纯文本会让模型
  凭空编技能名）。降级次数进 `llm_tool_mode_total{mode="unsupported"}`。

- **解析失败不再计入熔断**：`PlanValidationError` 表示「模型答了但不合规」，
  不是上游故障。原实现会把它算进熔断计数，几条离谱指令就能把 LLM 通道打成
  熔断，之后全部降级到规则规划。

### Added

- **技能库 15 → 21**：新增 `dock`、`undock`、`get_battery`、`set_led`、
  `capture_image`、`reset_odom`，mock 与 ROS2 双后端同名同参。

  几个刻意的细节：`dock` 的目标是「距桩心 `dock_clearance` 米」而不是桩心
  本身——充电桩在禁区里，开向桩心等于让机器人往禁区钻，会被自己的安全策略
  拦下；`capture_image` 的 `label` 走白名单正则，它会进文件名，不限制字符集
  等于把 `../` 交给模型填；`reset_odom` 只改里程计原点、不动物理位置
  （编码器清零，车没动）。

- **ROS2 Action 导航**（`skills/ros2_action.py`）：基于 `nav2_msgs/action/
  NavigateToPose` 封装点位导航，用 goal/feedback/result 三段替代「发速度再
  自己订阅 odom 猜到没到」。急停可取消 Action，不再只是发零速把车停住、
  任务还挂着。

  默认关闭（TurtleBot3 的 Gazebo 示例世界不带 Nav2），开启后 Action 不可用时
  回退 `/cmd_vel` 闭环；但 Action 明确 `aborted` / `rejected` 时**不**回退——
  规划器说这个目标到不了，再用比例控制硬冲一遍是把「规划失败」掩盖成
  「好像到了」。

- **设备级分布式锁**（`orchestration/execution_lock.py`）：`SET key token NX PX`
  一条命令完成「不存在才写 + 同时设过期」，释放走 Lua 比对 token 再删。

  两个必须做对的地方：`SETNX` + `EXPIRE` 拆成两条，中间崩了就没有 TTL，
  锁永远不释放；释放不比对 token，A 的锁过期后 B 拿到，A 执行完删掉的是 B 的
  锁——互斥静默失效，没有任何报错。

  默认 `lock.backend=none`：单实例的进程内信号量已经够了，加锁是纯开销。
  多副本 / CLI 与服务并存时必须开，否则两个进程会同时发 `/cmd_vel`。

- **LangGraph 原生 interrupt 审批**：`approval_mode=interrupt` 时在 screen 节点
  调用 `interrupt()`，图原地挂起，`Command(resume=...)` 从断点恢复。

  默认仍是 `persist`：`interrupt()` 依赖 checkpointer，MemorySaver 活在进程
  内存里，而审批是人的动作、可能等几小时——进程一重启 checkpoint 就没了，
  任务卡在「已挂起但无法恢复」。两种模式对外契约一致（都是
  `awaiting_approval`），批准后都重跑一次策略链。

  驳回也会唤醒挂起的图，否则 checkpoint 里会留着一条永不结束的线程。

- **意图识别路由**（`planner/router.py`）：`START → router → plan`，把指令
  分成 move / manipulate / query / system，结果进审计与日志。

  刻意的取舍：`unknown` **不拦截**，仍然交给规划器。关键词表必然漏词
  （「挪一下」「怼过去」都是合法指令），拿覆盖率有限的规则去拦，会把本来
  能完成的任务变成失败——比多花一次模型调用糟糕得多。

### Fixed

- Windows 下 `capture_image` 写 PPM 时文本模式把 `\n` 转成 `\r\n`，文件头
  字节数与 Linux 不一致。改用 `newline=""`。
- `test_registry_len` 的下限从 15 提到 21，作为「能力被砍」的护栏。

## [1.2.0] - 2026-11-02

主题：**把「拦得住」变成可执行、可复现、能被 CI 门禁的证据。**

### Added

- **安全评测集与 `robot-agent eval`**（`evaluation/`，L4）：82 条标注用例、18 个类别，
  覆盖越界、禁区、超速降速、危险表述、不可解析输入、部分解析、空输入、步数上限、
  边界语义，以及 LLM 幻觉产生的两类非法计划（不存在的技能名、枚举外的参数）。
  输出拦截召回率、误拦率、审批召回率、混淆矩阵与策略触发分布，`--json` 供 CI 消费。
  评测只做「规划 + 安全判定」，不执行技能，因此不需要 ROS/Gazebo，4 毫秒跑完。

  **为什么值得单独做一层**：`拦截率 99.2%` 这类数字，如果没有可复现的评测集，
  在技术评审里等同于没有。有了它，数字才有分母、有口径、有失败明细。

- **`known_gap` 用例语义**：期望放行、当前实现做不到的用例单独计数，不计入失败。
  三条缺口（相对位移、最近物体选择、模糊目标澄清）连同「为什么做不到、打算怎么补」
  一起写进数据集——一个从不失败的评测集，只说明出题的人只会出自己会做的题。

- **`ParamSchemaPolicy`**（第 7 条策略）：把技能参数校验从 `Skill.execute` 前移到闸门。
  非法计划不再变成任务记录，审计里留下的是「安全拒绝」而不是「执行异常」。
  这条策略是被评测集发现的：`invalid-color` 与 `invalid-missing-arg` 两条用例最初
  被判为「放行」，暴露出参数校验原本漏到了执行末端。

- **技能库从 4 个扩到 15 个**：新增 `move_relative` / `rotate` / `look_at` / `patrol` /
  `pick_cube` / `place_cube` / `open_gripper` / `close_gripper` / `report_status` /
  `wait` / `set_speed`。mock 与 ROS2 双后端实现同一组名字与参数，由
  `test_mock_and_ros2_share_skill_names` 守住一致性。

- **`skills/params.py`**：15 个技能的参数模型集中定义。同一份 schema 同时喂给 LLM
  函数描述、闸门校验、执行器入参，分散在三处必然漂移。

- **CI 新增「安全评测」job**：`robot-agent eval --fail-under 1.0`。改动策略链或默认配置
  导致行为漂移时立刻变红。

### Changed

- ROS2 执行器的导航与旋转统一为 odom 闭环，`rotate` 改用角速度闭环而非定时长；
  夹爪接口约定为 `/gripper/position`（0 闭合、1 张开）。
- `Skill.execute` 改为把**校验后的对象**传给 `run`。此前声明为 `list[GotoParams]` 的参数
  实际收到 `list[dict]`，类型标注是假的——`patrol` 技能上真实触发过这个 bug。
- `config/default.yaml` 的 `blocked_keywords` 补上「全速冲」：代码默认值里有它，YAML 漏了，
  导致同一句指令在两种加载路径下判定不同。

### Fixed

- 策略链遇到 `instruction=None` 时 `in None` 抛 `TypeError`，被 fail-closed 兜底成拒绝，
  会把「转人工审批」的用例误判成「已拦截」。
- README 与 CHANGELOG 中因编辑事故产生的重复段落。

### 已知缺口（如实保留）

- `pick_cube` / `place_cube` 在 ROS2 侧走「导航到方块旁 → 开关夹爪 → 依赖物理吸附」的
  降级路径，没有运动规划。固定场景可跑，杂乱场景不成立——真正的抓取需要 MoveIt2。
- 评测集是回归基线，不是泛化证明。这句话写进了 README，而不是藏在指标后面。

## [1.1.0] - 2026-11-02

### Changed

- **项目更名为 `robot-agent`**（原 `gazebo-langgraph-demo`）。名称里不再带 `demo`：
  交付形态是服务而非示例脚本，`pyproject` 名称、CLI、镜像名、systemd 单元本就叫
  `robot-agent`，这次把仓库名与文档链接统一过来，`gazebo` 只作为可选执行器后端出现在
  `ros2_ws/` 里。旧仓库地址由 GitHub 自动重定向。
- **凭据不再硬编码、不落仓库**：发布工具改为按「环境变量 `GH_TOKEN` → 权限 600 的凭据文件」
  顺序取 token，任何输出前统一脱敏；同步前先扫描工作区，命中 token 字面量直接拒绝上传。
- **抽取纯领域层 `domain/`**（`plan.py` + `state.py`）：`PlanStep`/`Plan` 原在 `planner/base.py`、
  `TaskRecord` 原在 `orchestration/state.py`，导致 `safety → planner`、`storage → orchestration`
  两条反向依赖。契约对象下沉后，四个使用方（规划/安全/编排/存储）只依赖模型本身。
  同时删除死代码 `runner.run_once`（唯一一处把组合根反向 import 进编排层的地方）。
  `planner` 与 `orchestration` 的公开导出保持不变，调用方无需改动。

### Changed（编排层单一职责）

- **报告渲染移出状态机**：`graph.py` 里 40 行的 `_render_report` 抽成 `reporting.py` 的纯函数。
  改一句文案不再需要跑一遍 LangGraph，四种分支（成功/拦截/待审批/规划失败）也能单独测。
  同时修正报告里的步骤序号从 0 起——给值班的人看「第 0 步」像是 bug。

### Changed（交付层单一职责）

- **CLI 拆出渲染层** `console.py`：表格、JSON、错误行、审计列表等纯字符串函数从命令函数里移出，
  命令只表达「做什么」。顺手修掉中英混排时**表格错位**——原实现用 `len()` 对齐，
  中文占两列却按一列算；现在按终端显示宽度（East Asian Width）补空格与截断。
- **仓储打开语义收进存储层**：`TaskRepository` 协议新增 `open()`（建立连接/建表），
  `sqlite` 的 `init()` 更名对齐；新增 `storage.session.open_repository()` 上下文管理器。
  CLI 三个子命令原本各自抄一遍「build → isinstance 判断 → init → try/finally close」，
  还把 `SqliteTaskRepository` 的实例判断泄漏到交付层，现在只写 `async with open_repository(url)`。

### Added

- **列表分页元数据**：`GET /v1/tasks` 返回 `total` / `limit` / `offset` / `has_more`，
  仓储协议补 `count(status)`（SQLite 走 `COUNT(*)`，内存走计数）。原先只回 `count=len(items)`，
  客户端只能靠「返回条数是否等于 limit」猜有没有下一页，最后一页整好整除时会多翻一次空页。

- **分层约束门禁**（`tests/unit/test_layering.py`）：AST 静态分析全部源码，校验「包必须登记层号 /
  依赖只能向下 / 领域层零外部依赖 / 独立纵切不互依赖 / 只有交付层可碰组合根 / 包图无环」六条规则。
  违规报错精确到 `robot_agent.skills (L2) 反向依赖 robot_agent.orchestration (L4)`。
- **仓库级密钥扫描**（`tests/unit/test_no_secrets.py`）：对全部文本文件匹配 GitHub PAT /
  云厂商长期密钥 / 模型服务密钥 / 私钥块，随 `pytest` 在 CI 每次提交上执行。
  凭据一旦进入 git 历史就只能改写历史 + 吊销重发，这道门禁比 code review 更可靠。

## [1.0.0] - 2026-11-02

从演示脚本重构为可交付服务。**这是一次不兼容重构**：模块路径由 `src/*.py` 平铺改为
`robot_agent.*` 包，调用方需按新接口迁移。

### Added

- **分层包结构**：`planner` / `safety` / `skills` / `orchestration` / `storage` / `api`，
  依赖方向单向，可 `pip install -e .` 安装并暴露 `robot-agent` 命令。
- **配置系统**：pydantic-settings 分层配置（env > .env > YAML > 默认值），
  密钥统一 `SecretStr`，`prod` 环境带启动护栏（禁无鉴权对外监听/禁关审计/禁内存存储）。
- **异常体系**：统一 `code` / `http_status` / `retryable`，安全拒绝显式不可重试。
- **安全策略化**：6 条独立策略（白名单/红线词/边界/禁区/限速/审批），支持注入自定义策略；
  策略自身异常按拒绝处理（fail-closed）。
- **技能参数模型**：用 pydantic 替代字符串字典 schema，`extra="forbid"` 拒绝 LLM 幻觉参数。
- **LLM 规划器健壮性**：硬超时、指数退避重试、熔断器（closed/open/half_open）、
  输出逐层校验、token 与耗时指标；失败可配置降级到规则规划。
- **执行器可靠性**：单步超时、瞬时故障重试、超时后自动急停、按 `task_id` 隔离的事件回调。
- **任务生命周期**：幂等键、CAS 状态迁移、人工审批/驳回/取消、SQLite 持久化、过期清理。
- **事件总线**：线程安全投递（`call_soon_threadsafe`）、队列背压丢旧保新、终态哨兵。
- **审计日志 v2**：哈希链防篡改、大小轮转、崩溃后自动续链、`audit verify` 校验命令。
- **可观测性**：structlog JSON 日志（trace_id 上下文）、Prometheus 指标（未装则 no-op）。
- **CLI**：`run` / `serve` / `tasks list|show` / `audit verify|tail` / `doctor` / `config` / `version`。
- **HTTP 控制面**（FastAPI）：任务提交/查询/列表、审批/驳回/取消、SSE 实时事件、
  `/healthz` `/readyz` `/metrics`、Bearer 鉴权、X-Trace-Id 透传、OpenAPI 文档。
  纯 ASGI 中间件避免 SSE 被缓冲；异常统一映射为稳定错误码。
- **优雅关闭**：先等在飞任务收尾（默认 10s）再释放资源，避免任务状态卡在 running。
- **请求体上限**：纯 ASGI 中间件按字节数拦截（默认 64 KiB，超限 413 `payload_too_large`），
  同时校验声明长度与实际读取量，chunked 传输与伪造 `Content-Length` 都挡得住。
- **急停通道**：`POST /v1/robot/stop` 与 `robot-agent estop`——不绑任务、不排物理动作信号量，
  直接下发零速并写审计；执行器缺 `stop` 技能时返回 503 而不是假报成功。
  mock 后端补齐 `stop` 技能（速度归零 + 命令历史留痕），使急停可在无 ROS 环境下测试。
- **事件总线资源边界**：单任务 SSE 订阅数上限（默认 32，超限 429 `too_many_subscribers`）；
  「已结束任务」集合改为定长环形缓冲（原先只写不读，跑久了就是一张永不释放的表）；
  任务结束后才订阅的客户端立刻收到结束哨兵，不再白等。
- **过期清理真正被执行**：`PurgeScheduler` 随应用 lifespan 起停，按
  `storage.purge_interval_s`（默认 30 分钟）调用 `retention_hours` 清理。
  此前该配置只写在文档里没人调用，数据库实际会无限增长；单轮失败只记日志，不中断后续轮次。
- **容器化**：多阶段 Dockerfile（非 root、只读根文件系统、HEALTHCHECK）、docker-compose。
- **部署清单**：systemd unit（加固项齐全）、K8s Deployment/Service/PVC/ServiceMonitor。
- **文档**：API 参考、架构说明（含失败隔离矩阵）、运维 Runbook（故障决策树 + 告警阈值）、
  两份 ADR（fail-closed、状态 CAS）、SECURITY 信任边界、CONTRIBUTING 扩展指南。
- **测试**：259 个用例，覆盖策略边界、fail-closed、CAS 并发、哈希链篡改、CLI 退出码、
  LLM 重试/熔断/降级、HTTP 接口与 SSE、优雅关闭。
- **质量门禁**：ruff（含 bandit/async/bugbear 规则）+ ruff format + mypy + GitHub Actions
  （3.10/3.11/3.12 矩阵、ROS 降级导入检查、镜像构建与容器冒烟）。
- **CI 新增两道防线**：「最小依赖可导入」job 只装核心依赖验证 CLI 与指标 no-op 降级；
  「ROS2 包结构与语法」校验 `package.xml` / `resource` 标记 / launch 入口。
- **ROS2 包**：`ros2_ws/src/robot_agent_ros2` 采用标准 `ament_python` 结构，
  `ros2 launch robot_agent_ros2 demo.launch.py` 一条命令拉起世界与三个方块；
  `spawn_cubes` 由 `TimerAction` 延时 5s，规避 `/spawn_entity` 服务未就绪的竞态。
- `tests/` 分三层：`unit`（策略/规则/技能/审计/存储/LLM）、`integration`（图/API）、`e2e`（CLI）。
- `Makefile` 提供 install/test/check/run/doctor 等常用命令。
- `config/default.yaml` 与 `config/production.yaml` 双环境示例；`.env.example` 说明全部变量。
- `pre-commit` 钩子：ruff、mypy、单元测试（pre-push）。

### Changed

- 规则规划器改为**括号感知分句**，修复坐标 `(1.0, 0.5)` 被逗号切断的问题。
- 方向目标点收缩 20% 边距，避免目标贴住物理围挡。
- 日志输出改到 **stderr**，stdout 留给结构化输出（`--json` 可被 `jq` 直接消费）。
- 仿真辅助脚本迁入 `ros2_ws`，README 的接入步骤改为 `colcon build` + `ros2 launch`。

### Fixed

- **Python 3.10 下 `asyncio.wait_for` 超时捕获不到**：3.10 的 `asyncio.TimeoutError` 不是内置 `TimeoutError`，`except TimeoutError` 会漏接——清理协程会被一次正常超时打死，同步等待也会变成 500。统一改为 `except asyncio.TimeoutError`（3.11+ 两者为同一别名）。
- **幂等键复用会静默返回别人的任务**：同键不同指令现在返回 `409 task_conflict`，`details` 带上原指令与原任务 ID；比较忽略空白差异。
- **过期清理会误删未完成任务**：`purge_expired` 原来只按 `created_at` 判断，超过保留期的 `awaiting_approval` / `running` 任务会连同待办一起消失。现在只清终态。
- **`retention_hours` 从未被真正执行**：新增 `PurgeScheduler` 与 `robot-agent tasks purge`。
- **CI 三处失败**（44cefd7）：`typer` 未声明为依赖导致测试环境 collection 失败；
  `tests/unit/test_storage.py` 仍用 3.11+ 的 `datetime.UTC`；无 `prometheus-client` 时
  no-op 桩不吞构造参数，`Counter(...)` 直接 `TypeError` 把 mock 模式拖死。
- **依赖分层纠正**：`aiosqlite` / `pyyaml` 移入核心依赖（CLI 默认就要用），
  `fastapi` / `uvicorn` / `prometheus-client` 留在 `api` extra。
- **配置文件读不到不再静默忽略**：显式指定或自动发现的 YAML 缺失、非映射、缺 PyYAML
  时抛 `ConfigurationError`——否则限速与禁区策略没加载，人还以为生效了。
- `datetime.UTC` 仅 3.11+ 可用，改用 `timezone.utc` 以支持 3.10。
- structlog 保留字段 `event` 作为 kwargs 导致 `TypeError`。
- CLI `standalone_mode=False` 时退出码被丢弃，导致失败任务返回 0。
- `.gitignore` 误排除 `docs/*.gif`，演示素材无法入库。

### Removed

- `src/audit.py` / `src/graph.py` / `src/planner.py` / `src/safety.py` / `src/skills/*`
  的平铺模块（迁移到 `src/robot_agent/` 对应子包）。
- 根目录 `launch/demo.launch.py` 与 `scripts/spawn_cubes.py`（迁入 `ros2_ws` 的 ament 包）。
- 根目录 `scripts/demo.py`（已被 `robot-agent run` 取代，且引用了已删除的模块）。
