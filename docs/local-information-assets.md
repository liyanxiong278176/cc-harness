# 本地上下文、记忆和 Skill

WebUI 侧栏的“本地能力与记忆”提供当前项目的记忆查看与遗忘、Skill 启停、上下文来源、引用重新绑定和清理预览。不需要配置模型账户即可查看已有资产；两种入口共用 `LocalAssets` 服务。

## 命令入口

```powershell
cc-harness --cwd D:\projects\demo --command assets
cc-harness --cwd D:\projects\demo --command assets --run-id RUN_ID
cc-harness --cwd D:\projects\demo --command assets --asset-action forget --asset-id MEMORY_ID
cc-harness --cwd D:\projects\demo --command assets --asset-action enable --asset-id project:dependency-check
cc-harness --cwd D:\projects\demo --command assets --asset-action disable --asset-id project:dependency-check
cc-harness --cwd D:\projects\demo --command assets --asset-action reload --run-id RUN_ID
cc-harness --cwd D:\projects\demo --command assets --asset-action bind --run-id RUN_ID --asset-id NODE_ID --asset-path evidence\original.txt
cc-harness --cwd D:\projects\demo --command assets --asset-action cleanup-preview
cc-harness --cwd D:\projects\demo --command assets --asset-action cleanup --preview-token PREVIEW_TOKEN
```

命令不启动模型或重新执行任务工具。指定 Run ID 时展示该 Run 的资料；WebUI 会沿当前会话定位当前执行的 Run。清理候选变化时旧 token 失效，需要重新查看预览。

## 续行与上下文策略

每轮从权威目标、计划、工作状态、纠偏、审批和工具事实构建续行资料，写到：

```text
.cc-harness/context/<run-id>/CONTINUATION.json
.cc-harness/context/<run-id>/CONTINUATION.md
```

这些文件可重建。修改它们不会更改任务事实；调整目标应通过正常消息或任务纠偏入口。未知工具结果必须先核查，恢复资料不授权重放副作用。

项目 `policy.yaml` 的 `context` 段提供轻量策略，既有 `CONTEXT_*` 环境变量优先覆盖：

```yaml
context:
  policy_version: local-context@1
  integrity_check_enabled: true
  protect_zone_tokens: 8192
  output_reserve_tokens: 8192
  tier1_threshold: 0.6
  tier2_threshold: 0.8
  tier3_threshold: 0.95
memory:
  verified_auto_memory: true
```

默认使用已有模型窗口配置，不必在项目文件硬编码窗口。调用 Manifest 记录策略摘要、实际项目规则来源、工具 Schema、Skill 版本、记忆快照和历史内容的变换/遗漏原因。续行完整性失败先做一次有界重建；仍缺证据或超过预算时等待用户处理，不静默截去必要约束。

## 长期记忆与遗忘

自动提取必须提供来源索引与逐字摘录。用户原话可支持稳定偏好或明确决策；项目事实需要成功的规范化工具观察。模型推测、临时进度、密钥和审批授权不进入该自动事实管线。自动事实保留来源、验证时间和项目范围，始终作为建议性参考。

启用来源验证时，不向模型提供任意写入记忆的 `memory_save` 工具，也不将生成式合并或反思结论直接发布为长期事实；显式 benchmark 历史保留模式维持独立契约。

遗忘会撤回相关版本、合并派生项、检索索引、场景、画像及自动注入快照，并阻止从同一来源再次提取。为避免混合摘要残留，高层视图可能整体失效，再从剩余原子事实重建。原始聊天、工具记录和独立执行证据保留，已交给正在运行的模型的输入无法撤回；后续调用会刷新记忆视图。

原始历史清除是现有会话/项目生命周期的独立操作，不能将“遗忘”当作销毁历史或安全擦除磁盘的证明。

## 本地 Skill

```text
<project>/.cc-harness/skills/<directory>/SKILL.md
<user-home>/.cc-harness/skills/<directory>/SKILL.md
```

用户级目录可由 `CC_HARNESS_HOME` 改为 `<CC_HARNESS_HOME>/skills`。两个来源均须明确启用；同名来源的选择不会静默叠加。基本格式：

```markdown
---
name: dependency-check
version: "1"
description: 检查依赖变更并整理测试证据。
required_tools: [Read, run_command]
required_environment: []
---
读取项目规则，运行 scripts/verify.py，然后核查结果并报告证据。
```

首次准备任务时，指令和资源一起固定，并用 `SkillSnapshotPinned` 事件记录选择。发现层只披露简短目录；模型通过只读 `read_skill` 按需读取。该工具返回固定资源目录，脚本执行应使用该目录，并仍通过正常动作与权限检查。

修改源文件默认对后续任务生效，恢复任务继续使用原版本。显式重新加载要求任务不处于运行、排队或取消处理中，并记录新版本。包外符号链接、非法资源路径、缺少工具/环境依赖和超出本地资源预算的包会被拒绝。固定快照不会让当前模型获得更多执行权限。

## 引用、产物与清理

项目移动后，读取引用沿用已有的项目内路径重定位；失效时可指定当前项目内文件。只有字节摘要完全一致才能恢复历史引用，不能用新版文件替换旧证据，也不会扫描整台电脑。

工作区产物视图记录当前文件摘要、所属 Run 和验证证据。文件写出不等于验证完成；只有验证结果与该内容摘要关联，才标记已验证。当前工作区文件与历史快照分开描述，不建立企业发布状态机。

清理仅针对超过宽限期的未提交孤立对象；事件、检查点、固定 Skill 以及 Manifest 的嵌套引用均受保护。输入保留来源、临时文件按显式操作处理，权威状态从事件恢复，有效产物和证据按引用保留。此功能不批量删除用户工作区文件。
