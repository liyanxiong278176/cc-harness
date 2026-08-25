"""Single source of truth for slash command help and completion."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description_en: str
    description_zh: str


COMMANDS = (
    CommandSpec("/help", "Show commands", "显示所有可用命令"),
    CommandSpec("/init", "Create CC-HARNESS.md project instructions", "创建项目指令文件 CC-HARNESS.md"),
    CommandSpec("/release-notes", "Show cc-harness release notes", "查看 cc-harness 版本更新记录"),
    CommandSpec("/status", "Show session status", "查看当前会话、模型和运行状态"),
    CommandSpec("/clear", "Clear conversation context", "清除当前对话上下文，保留系统指令"),
    CommandSpec("/resume", "Select a saved session", "选择并恢复历史会话"),
    CommandSpec("/branch", "Fork this conversation into a new session", "从当前会话创建独立分支"),
    CommandSpec("/rename", "Rename the current session", "重命名当前会话"),
    CommandSpec("/exit", "Save and exit", "保存当前会话并退出"),
    CommandSpec("/coding", "Switch to coding mode", "切换到编码工作模式"),
    CommandSpec("/plan", "Switch to plan mode", "切换到计划工作模式"),
    CommandSpec("/design", "Switch to design mode", "切换到设计工作模式"),
    CommandSpec("/chat", "Switch to chat mode", "切换到聊天工作模式"),
    CommandSpec("/mode", "Show current work mode", "查看当前工作模式"),
    CommandSpec("/model", "Show or set the model", "查看或设置当前模型"),
    CommandSpec("/effort", "Show or set reasoning effort", "查看或设置推理强度"),
    CommandSpec("/permissions", "Show or set permission mode", "查看或切换工具权限模式"),
    CommandSpec("/verbose", "Toggle detailed output", "切换详细输出模式"),
    CommandSpec("/context", "Show context usage", "查看上下文占用和剩余容量"),
    CommandSpec("/usage", "Show API token/cache usage", "查看 API Token、缓存命中和实时费用"),
    CommandSpec("/compact", "Compact conversation context", "压缩对话上下文，减少 Token 占用"),
    CommandSpec("/tools", "List available tools", "列出当前可用工具"),
    CommandSpec("/mcp", "Show MCP status", "查看 MCP 服务连接状态"),
    CommandSpec("/rewind", "Restore a conversation/file checkpoint", "恢复到历史对话和文件检查点"),
    CommandSpec("/focus", "Show only the current turn", "只显示当前回合内容"),
    CommandSpec("/diff", "Show files changed this session", "查看本次会话修改的文件差异"),
    CommandSpec("/tasks", "Show real task state", "查看任务列表及执行状态"),
    CommandSpec("/agents", "Show real agent state", "查看子代理任务及状态"),
    CommandSpec("/inspector", "Open the run inspector", "打开运行检查器"),
    CommandSpec("/tui", "Show or select the terminal renderer", "查看或切换终端渲染器"),
)

COMMAND_MAP = {command.name: command for command in COMMANDS}
