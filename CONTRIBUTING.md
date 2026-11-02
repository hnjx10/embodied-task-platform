# 贡献指南

## 开发环境

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,api]"
pre-commit install

make check        # lint + type + test，提交前必跑
```

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```
feat(safety): 增加夜间禁行策略
fix(planner): 修复坐标被逗号切断
docs: 补充 SSE 断连处理说明
refactor(orchestration)!: 重构任务状态机     # ! 表示破坏性变更
```

类型：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `perf`。

## PR 要求

1. **测试**：新逻辑必须有测试；修 bug 必须先写一个能复现的失败测试。
2. **覆盖率不下降**：CI 门槛 75%。
3. **公共接口变更**要更新 `CHANGELOG.md` 与相关文档。
4. **架构级决策**（引入依赖、改变分层、调整信任边界）请加一篇 ADR 到 `docs/adr/`。

## 新增一个技能

```python
# src/robot_agent/skills/mock.py
class GripParams(SkillParams):
    object_id: str = Field(description="物体 ID")

class MockGripSkill(Skill):
    name = "grip"
    description = "抓取指定物体"
    params_model = GripParams
    risk = "high"           # 高风险 → 触发人工审批
    is_motion = True

    def run(self, object_id: str) -> SkillResult:
        return SkillResult(ok=True, detail=f"已抓取 {object_id}")
```

然后加进 `build_mock_registry()`。**不需要**改编排层、安全层或 API。

注意：
- `description` 会直接给 LLM 看，要写清楚「做什么」而不是「怎么实现」；
- `params_model` 的字段描述与类型同样会进 prompt，用 `Literal` 表达枚举；
- `risk="high"` 会自动触发审批策略，不要为了省事把物理接触动作标成 low。

## 新增一条安全策略

```python
class NightShiftPolicy:
    name = "night_shift"

    def evaluate(self, step: PlanStep, ctx: PolicyContext) -> PolicyDecision:
        if is_night() and step.skill in {"push_cube", "goto_point"}:
            return PolicyDecision.deny(self.name, "夜间禁止移动类动作")
        return PolicyDecision.pass_(self.name)
```

加入 `default_policies()` 的合适位置（顺序即优先级）。

**必须写两个测试**：正常放行、正确拒绝；再写一个「策略抛异常时应被当作拒绝」的测试。

## 测试约定

- 用真实组件装配（真 SQLite、真审计文件、真 LangGraph），只把物理世界换成 mock；
- 不要 mock 自己写的业务类——那会让测试退化成自我确认；
- 异步测试用 `pytest-asyncio` 的 auto 模式，直接写 `async def test_xxx`；
- 涉及时间的测试用 `monkeypatch` 或可注入的时钟，不要 `sleep` 太久。

## 代码风格

- 行宽 100，`ruff format` 为准；
- 注释写**为什么**，不写**做什么**（后者看代码）；
- 公共函数的 docstring 说明约束与副作用，例如"此方法会写审计"；
- 异常用 `robot_agent.errors` 里的类型，不要裸 `Exception`。
