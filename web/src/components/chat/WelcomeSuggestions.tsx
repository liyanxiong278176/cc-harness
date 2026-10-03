import { BookOpen, FileText, Play, Search } from 'lucide-react'
import './WelcomeSuggestions.css'

const suggestions = [
  {
    title: '讲解这个项目',
    detail: '梳理目录和核心模块',
    icon: BookOpen,
    prompt: '浏览项目结构，讲解核心模块及主要调用路径。先读取现有代码和文档，不要修改文件。',
  },
  {
    title: '跑测试并修复',
    detail: '定位失败并验证修复',
    icon: Play,
    prompt: '运行与当前项目相关的测试。若测试失败，定位原因、修复问题，并重跑受影响的测试。',
  },
  {
    title: '代码审查',
    detail: '查找风险和回归问题',
    icon: Search,
    prompt: '审查当前项目的未提交改动，优先检查正确性、安全性和回归风险。只报告有证据的问题。',
  },
  {
    title: '写一份方案',
    detail: '先调查，再列实施和验证步骤',
    icon: FileText,
    prompt: '根据当前项目代码和文档，为我写一份简明实施方案，说明关键决策、风险和验证步骤。',
  },
]

export function WelcomeSuggestions({ onChoose }: { onChoose: (prompt: string) => void }) {
  return (
    <div className="welcome-suggestions" aria-label="建议任务">
      {suggestions.map(({ title, detail, icon: Icon, prompt }) => (
        <button type="button" className="welcome-suggestion" key={title} onClick={() => onChoose(prompt)}>
          <span className="welcome-suggestion-icon"><Icon size={17} /></span>
          <span className="welcome-suggestion-copy"><strong>{title}</strong><small>{detail}</small></span>
        </button>
      ))}
    </div>
  )
}
