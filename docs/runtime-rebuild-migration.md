# Durable Runtime 迁移与运行

重建后的默认入口是 durable runtime；`--runtime legacy` 只用于迁移期回退。旧
`.cc-harness` 数据不会被删除或覆盖。

先只读检查真实项目来源：

```powershell
.venv\Scripts\python.exe scripts\check_runtime_rebuild_migration.py `
  --project-root D:\agent_learning\cc-harness --dry-run
```

导入旧 session、Todo、action journal 和 memory SQLite：

```powershell
.venv\Scripts\python.exe scripts\check_runtime_rebuild_migration.py `
  --project-root D:\agent_learning\cc-harness
```

导入是按 source digest 幂等的；重复运行不会重复事件。旧文件仍保留在原处，
新事件和对象位于按 project identity 解析的用户数据目录。
迁移出来的旧 session 默认标记为 `blocked`，不会被 supervisor 当作新任务自动
执行；确认目标后对相应 run 使用 `resume` 才会重新入队。

提交任务只写入 coordinator queue：

```powershell
cc-harness --runtime durable "实现一个长期任务"
```

另开一个终端启动本机 supervisor，终端关闭不会取消已提交的 run：

```powershell
cc-harness --runtime durable --command supervisor --cwd D:\agent_learning\cc-harness
```

常用控制命令：

```powershell
cc-harness --runtime durable --command status --run-id <run-id>
cc-harness --runtime durable --command list
cc-harness --runtime durable --command interrupt --run-id <run-id>
cc-harness --runtime durable --command cancel --run-id <run-id>
```

总 token、费用、调用次数和时长没有硬预算上限；worker 只使用动作超时、lease、
并发、存储、审批和停滞保护。
