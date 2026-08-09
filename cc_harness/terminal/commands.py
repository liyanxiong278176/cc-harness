"""Single source of truth for slash command help and completion."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description_en: str
    description_zh: str


COMMANDS = (
    CommandSpec("/help", "Show commands", "显示命令"),
    CommandSpec("/init", "Create CC-HARNESS.md project instructions", "创建 CC-HARNESS.md 项目指令"),
    CommandSpec("/release-notes", "Show cc-harness release notes", "显示 cc-harness 更新记录"),
    CommandSpec("/status", "Show session status", "显示会话状态"),
    CommandSpec("/clear", "Clear conversation context", "清空对话上下文"),
    CommandSpec("/resume", "Select a saved session", "选择历史会话"),
    CommandSpec("/branch", "Fork this conversation into a new session", "分支当前会话"),
    CommandSpec("/rename", "Rename the current session", "重命名当前会话"),
    CommandSpec("/exit", "Save and exit", "保存并退出"),
    CommandSpec("/coding", "Switch to coding mode", "切换到编码模式"),
    CommandSpec("/plan", "Switch to plan mode", "切换到计划模式"),
    CommandSpec("/design", "Switch to design mode", "切换到设计模式"),
    CommandSpec("/chat", "Switch to chat mode", "切换到聊天模式"),
    CommandSpec("/mode", "Show current work mode", "显示当前工作模式"),
    CommandSpec("/model", "Show or set the model", "显示或设置模型"),
    CommandSpec("/effort", "Show or set reasoning effort", "显示或设置推理强度"),
    CommandSpec("/permissions", "Show or set permission mode", "显示或设置权限模式"),
    CommandSpec("/verbose", "Toggle detailed output", "切换详细输出"),
    CommandSpec("/context", "Show context usage", "显示上下文使用量"),
    CommandSpec("/compact", "Compact conversation context", "压缩对话上下文"),
    CommandSpec("/tools", "List available tools", "列出可用工具"),
    CommandSpec("/mcp", "Show MCP status", "显示 MCP 状态"),
    CommandSpec("/rewind", "Restore a conversation/file checkpoint", "恢复对话和文件检查点"),
    CommandSpec("/focus", "Show only the current turn", "仅显示当前回合"),
    CommandSpec("/diff", "Show files changed this session", "显示本会话文件变更"),
    CommandSpec("/tasks", "Show real task state", "显示真实任务状态"),
    CommandSpec("/agents", "Show real agent state", "显示真实代理状态"),
    CommandSpec("/tui", "Show or select the terminal renderer", "显示或选择终端渲染器"),
)

COMMAND_MAP = {command.name: command for command in COMMANDS}
