# cc-harness eval 工作区

本目录保留三个评测/观测平台的**调研文档**,不安装任何运行时依赖。

## 当前状态(2026/06)

| 子目录 | 平台 | 状态 |
|---|---|---|
| `promptfoo/` | promptfoo | 📄 仅文档,**已卸载** |
| `deepeval/`  | DeepEval  | 📄 仅文档,**已卸载** |
| `langfuse/`  | Langfuse  | 📄 仅文档,**从未启用** |

## 平台选型记录

| 平台 | 结论 | 原因 |
|---|---|---|
| **promptfoo** | ✅ 选型通过 | CLI + YAML + OWASP 红队白嫖。已验证可用,但目前不需要 |
| **DeepEval** | ✅ 选型通过 | pytest 原生集成 + LLM 当 judge。已验证 5/5 metric 通过,但目前不需要 |
| **Langfuse** | ⏸️ 未启用 | v3 自托管需要 Postgres + ClickHouse + Redis + web 4 个容器,部署成本太高 |
| Braintrust | ❌ | 商业优先,免费 tier 早晚缩 |
| LangSmith | ❌ | LangChain 专属,跟 cc-harness 不匹配 |
| Arize Phoenix | ⚠️ 备选 | 跟 Langfuse 重叠,一行 `pip install + phoenix serve` 比 Langfuse 轻 |
| Laminar | ⚠️ 观望 | 太新,文档少 |
| Latitude | ❌ | GEPA 核心在付费层 |

## 为什么三个都没实际启用

经过实际跑了一遍 promptfoo + DeepEval + 试图配 Langfuse,得出的结论:

1. **promptfoo / DeepEval 直接调 LLM judge,不走 cc-harness ReAct loop**
   - 它们测的是"给 LLM 这条 prompt 它怎么回"
   - **不是**测 cc-harness 的 tool 调用、memory 召回、context 压缩等核心行为
   - 真正想测这些,用现有的 217 个单元测试 + `_test_*.py` 集成测试

2. **Langfuse 自托管部署复杂**
   - v3 需要 ClickHouse + Redis + Postgres,Windows Docker Desktop 默认配置有坑
   - 暂时不需要 trace 回放,日志够用

3. **观测需求没起来**
   - 单人开发,跑的次数有限
   - 出问题的时候打日志就够了,不需要专门 trace 平台

## 何时再启用

- 用户量/调用量上来,需要看 trace
- 出现具体 bug 需要按时间线回放
- 想做 CI 门禁(红队 + metric 自动化)

到时重新 `npm install -g promptfoo` + `pip install deepeval` 即可,YAML/Python 配置文件参考各子目录 README 里的存档内容(已写在 README 里)。

## 维护

新增评测/观测工具时:
- 新建 `eval/<tool-name>/`
- 在该目录下写 `README.md`(调研结论 + 决策)
- 如果决定启用,再加 `promptfooconfig.yaml` / 测试代码 / 启动脚本

不启用就**只留 README**,避免污染项目依赖。
