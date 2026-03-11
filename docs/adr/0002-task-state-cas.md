# ADR 0002：任务状态迁移用 CAS，而不是读-改-写

- 状态：已采纳
- 日期：2026-11-02

## 背景

任务状态机存在多个并发写入者：后台执行线程、HTTP 审批请求、取消请求、过期清理。
最危险的场景是**两个人同时批准同一个待审批任务**。

朴素的实现是：

```python
task = await repo.get(task_id)          # 读到 awaiting_approval
if task.status == "awaiting_approval":
    await repo.save(task.status = "running")   # 两个人都写成功
    await execute(task)                        # 动作被执行两次
```

对机器人而言，「执行两次」意味着推两次箱子、导航两次，后果不可接受。

## 决策

所有状态迁移必须带**期望的前置状态**，由存储层原子地判断并更新：

```python
async def transition(task_id, *, expected: str, new: str, patch: dict | None) -> bool
```

- SQLite 实现：`UPDATE tasks SET status=? ... WHERE id=? AND status=?`，按 `rowcount` 判定；
- 内存实现：在 `asyncio.Lock` 内比较并替换。

返回 `False` 表示「状态已被别人改过」，调用方抛 `TaskConflictError`（HTTP 409）。

## 理由

1. 唯一性由存储层保证，不依赖调用方的谨慎；新增调用点无法绕过。
2. 冲突是**显式的**：调用方拿到 409 而不是悄悄覆盖别人做的决策。
3. 无需分布式锁——单机 SQLite 的 `UPDATE ... WHERE` 就是原子的，成本为零。

## 影响

- `transition` 的 `expected` 必须是**精确的单一状态**，不允许 `expected in (...)`，
  否则又退化成有竞态的条件更新。
- 需要「从多个可能状态取消」时，按状态逐个尝试 CAS，全部失败才算冲突
  （`TaskRunner.cancel` 就是这么做的）。
- 任务 ID 唯一 + 幂等键唯一索引，共同保证「重复提交不会产生第二个任务」。

## 替代方案

| 方案 | 为什么没选 |
|---|---|
| 进程内互斥锁 | 多进程/多实例部署失效；且锁的粒度难把握 |
| 数据库行锁（SELECT FOR UPDATE） | SQLite 不支持；且需要长事务，与异步代码冲突 |
| 版本号乐观锁 | 效果等价但要多维护 `version` 列；状态本身已经是天然的版本 |
