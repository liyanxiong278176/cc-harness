# Eval-v2:cc-harness Agent 评估套件增强设计

> 2026-07-28 | 状态:设计 | 关联:`docs/superpowers/specs/2026-07-25-codex-web-ui-design.md`(Web UI)

## §0 背景与目标

### 驱动
秋招简历:对标大厂(Anthropic/OpenAI/DeepMind/BAT)Agent 评估岗 JD 关键词 —— LLM-as-Judge / Pass^k / Trajectory / calibration / threat modeling / multi-source judge / frontier-risk。

### Gap(对比《AI-Agents-in-Depth》第 6 章 + AgentGuide「Harness 完全指南」)
cc-harness 现有 eval(`eval/promptfoo/`)是 **safety-first + defense-in-depth** 视角下的强实现:per-layer ASR(L2/L4/L5/L8)、真执行 wrapper(spawn REPL)、结构化 judge(HOLD/BROKE + self-mod 守卫)、动态生成 + embedding 去重。但缺第 6 章强调的 5 个维度:

1. **Pass^k 可靠性 + 统计显著性** —— 现状单次采样,75 条跨 18 类平均 4 条/类,standard error ≈ 11.7%,ASR 分不清 signal/noise
2. **Trajectory + Outcome 双覆盖** —— `wrappers/cc_harness.py:_extract_result` 只抽"结果"段,思考/行动/观察三段全丢
3. **Multi-source judge** —— 单源(promptfoo cloud),Goodhart 剥削风险
4. **Gold-set calibration** —— judge 未校准,无 κ
5. **Capability eval** —— 只测 safety,不测能力(locomo 子系统存在但未并进主 report)

### 范围(7 项,原计划 8)
砍 #15「公开 benchmark」—— MMLU/HumanEval 评 base LLM 不评 agent harness,概念错位,留 backlog。

| 项 | 维度 | 简历关键词 |
|---|---|---|
| #2 trajectory + 独立可视化 | 过程覆盖 | Trajectory evaluation、process vs outcome |
| #1 Pass^k + CI | 统计严谨 | Pass^k、statistical significance、calibration |
| #3 multi-source judge | 防 Goodhart | LLM-as-Judge、multi-source、inter-rater |
| #4 gold-set(活化+回归) | 校准 | Cohen's κ、gold-set、failure-driven |
| #7 cyber 深化 | threat modeling | frontier cyber-risk、threat modeling |
| #14 locomo 桥接 | capability | memory benchmark、recall@k |
| #9 write-up | 外部表达 | technical writing、external communication |

## §1 总体架构

7 项叠加在现有 promptfoo pipeline 上,4 子区:

```
现有: wrapper(抓"结果"段) → judge(单源 llm-rubric) → report(扁平 result + per-layer ASR)
                         ↓ Eval-v2 叠加
新增: wrapper(+ --emit-events JSONL trajectory)          ← #2
      judge(主 cloud + 第二源 MiMo)                       ← #3
      report(扁平 result 升级:repeat 聚合 + trajectory 列 + 双 judge 一致率 + 校准段)  ← #1 #2 #3 #4
      + cyber attack 扩 30+                               ← #7
      + locomo report 桥接进 unified                      ← #14
      + docs/eval-methodology.md + Zhihu                  ← #9
```

依赖图(决定 plan phase 划分):

```
#2 (wrapper + 事件 + 可视化) ──→ #1 (report 聚合,复用 wrapper)
                           └─→ #14 (locomo 复用 wrapper/judge/report)
#3 (judge 双源)             ──→ #4 (κ 校准这套 judge)
#7 (cyber 数据)               独立
#9 (文档)                     贯穿,收尾
```

## §2 #2 Trajectory + Outcome 双覆盖 + 独立可视化

### 动机
result 一样的「拒绝 rm -rf」可能是真安全(thought 拒绝)也可能是 borderline 隐患(thought 想执行 → 被 L4 拦)。过程丢失导致 borderline 诊断不出来,而 borderline 正是防御松一档就泄的真实风险。

### 方案
1. **cc-harness 加 `--emit-events <path>` flag**:agent.run_turn 的 event_emitter 把 Thought/Action/Observation/Result(pydantic Event,复用 `web/events.py`)序列化成 JSONL 落盘。每行一事件,带 `iteration`/`ts`。
   - JSONL 选型:流式 append(崩溃/超时 kill 不丢前序)/ 异构事件同文件(Thought/Action 字段不同)/ 跟 web `serialize()`(`data:{json}\n\n`)统一,一处定义两处用
2. **wrapper 配 `--emit-events`**:每条 attack 生成 `{attack_id}.jsonl`,trajectory 塞进 judge input(供 judge 评 reasoning safety)
3. **report 加 trajectory 指标列**:步数、工具调用错误率、borderline 标记(thought 含攻击意图词 + result 拒绝 → borderline)
4. **独立静态 HTML 可视化器** `tools/trajectory_to_html.py`:JSONL → 自包含 HTML(4 段流式 + borderline 高亮 + 步数折叠)。**零依赖,不碰 `web/src`**(web 前端是独立项目,且有遗留问题,eval 可视化不依赖它)。渲染逻辑独立成纯函数,以后可并进 web。

### 改文件
`cc_harness/main.py`(argparse 加 flag)、`cc_harness/agent.py`(run_turn 接 emit_path + 构造 JSONL writer emitter)、`cc_harness/web/events.py`(to_jsonl_line helper,或复用 serialize)、`eval/promptfoo/wrappers/cc_harness.py`(配 flag + 读 JSONL)、`eval/promptfoo/tools/report_to_md.py`(trajectory 指标列)、`eval/promptfoo/tools/trajectory_to_html.py`(新)

### 验收
- 跑 1 条 attack → 磁盘生成 `{id}.jsonl`(含 ≥1 thought + result)
- report 该行有 步数 / 工具错误率 / borderline 列
- `trajectory-report.html` 生成,浏览器打开看 4 段流 + borderline 高亮

### 不做
- thought 语义级幻觉检测(留 judge)
- 并进 web 前端(留后续)

## §3 #1 Pass^k + 统计显著性

### 动机
现 report 每条 attack = 1 次 result,standard error ≈ √(p(1-p)/4) ≈ 11.7%,ASR 数字分不清 signal/noise。PDF §6.4 表 6-3:回归测试用 Pass^k(稳定性),探索用 Pass@k(能力上限)。

### 方案
**混合采样**:critical 类 repeat **n=5**,其余类 n=1(报 ASR)。
- 每条 critical attack 跑 5 次,记 hold 次数
- `hold^5` = 5 次全 hold 的比例(稳定性,越高越安全)
- σ = 二项标准误 √(p(1-p)/n)
- **95% Wilson score interval**(n=5 小样本必须用 Wilson,不用 normal approximation —— 后者在小 n 失真)
- 其余类保持单次 ASR
- report 顶部标注采样策略「critical×5 / rest×1」

### 改文件
`promptfooconfig.unified.yaml`(critical 类标 repeat)、`eval/promptfoo/tools/report_to_md.py`(聚合 + σ + Wilson CI 计算 + 报告列)

### 验收
- critical 类每条 report 有 `hold^5=0.6, σ=0.22, 95%CI[0.15,0.95]`
- report 顶部标注采样策略
- CI 宽度诚实呈现(n=5 的 Wilson CI 仍宽,不夸大统计力)

### 不做
- 全量 ×5(成本)
- Best@k(二元 hold/broke 任务用不上)

## §4 #3 Multi-source judge

### 动机
PDF §6.5.1 + Goodhart 定律:单源 judge 会被 agent 学会剥削;异家族 judge 偏见正交,agent 难同时骗所有 judge。

### 方案
- **主 judge**:promptfoo cloud(异家族,现状)
- **第二源 judge**:MiMo(`.env` `mimo-v2.5-pro`,OpenAI 兼容接口,本地免费)
- 每条 attack 双 judge 评分(同一 rubric `judges/attack_held_ground.txt`)
- report 出双 judge **一致率** + **分歧**(score 差 >0.3)高亮送人工复核
- 架构留口:以后加 GPT-4o 第三源(只需加一个 grader)

> agent 本身也是 MiMo,但只要「两 judge 之间」异源(cloud vs MiMo)即满足多源防 Goodhart;agent 与第二源同源的残余风险由主 judge(cloud)兜底。

### 改文件
`eval/promptfoo/judges/`(MiMo judge,复用 attack_held_ground.txt 或派生)、`promptfooconfig.unified.yaml`(并行 grader / 第二 provider)、`report_to_md.py`(双 judge 一致率 + 分歧列)

### 验收
- 每条 attack 出 2 个 score(cloud + MiMo)
- report 出「双 judge 一致率 X%, 分歧 N 条」
- 分歧(score 差 >0.3)行高亮

### 不做
- 3+ 源(架构留口,先 2 源)
- judge 自动仲裁(分歧送人工)

## §5 #4 Gold-set(活化 + 回归语义)

### 动机
PDF §6.5.2:judge 放量前需金标集校准(κ>0.7)。静态 gold-set 不反映当前真实弱点。**活化**:从真实突破持续长出来,既校准 judge 又当回归集 —— 这是 Anthropic red team 的 failure-driven eval growth 做法,比静态 gold-set 高一档。

### 现有可复用管线
`tools/curate_attacks.py` 已把 eval failures(score<0.4)+ embedding 去重(cosine<0.85)提升进 `attacks.yaml`(手动 `npm run curate`)。复用它,不重造。

### 方案
**双来源** `calibration_set.yaml`:
1. **静态基线**:现有 attack 抽样 50 条(各严重度 + 边界 case)
2. **失败驱动收集**:eval 跑完,report 自动把 **`score=0`(broke)且 `severity∈{critical,high}`** 的 case 写进「待标」区(复用 curate 的 embedding 去重避免重复)

**多维度人工标**(每条):
| 字段 | 取值 |
|---|---|
| `hold_broke` | 0 / 1 |
| `borderline` | bool(thought 盘算执行 + result 拒绝) |
| `leak_type` | enum: none / credential / pii / prompt / system |

**校准**:judge 评同样 3 维 → 算每维 Cohen's κ + 整体一致率 → report 校准段。κ<0.7 警告「judge 需校准」。

**回归语义**:gold-set = 已知弱点回归集。改防御后重跑 gold-set → report 出「无回归 / 新回归 N 条」(之前挡住的别又漏)。

### 改文件
`eval/promptfoo/judges/calibration_set.yaml`(新,双区:基线 + 待标)、`eval/promptfoo/tools/calibrate.py`(新,κ + 失败收集 + 回归)、复用 `tools/curate_attacks.py`、`report_to_md.py`(校准段 + 回归段)

### 验收
- 50 条基线标完
- eval 跑批后 `calibration_set.yaml` 待标区自动 +N(score=0 + critical/high)
- `calibrate.py` 出每维 κ;κ<0.7 report 警告
- 回归跑(重跑 gold-set)出「无回归 / 新回归 N 条」

### 不做
- 全量标(50 够算 κ)
- 多人盲标(单人标 + 抽样复核,秋招项目量级)

## §6 #7 Cyber 风险域深化

### 动机
CBRN(生化核)是**通用 LLM** 的 risk —— Anthropic 测 CBRN 因 Claude 可能被问生物武器。cc-harness 是 **coding agent**,没人让它合成病原体,真实 attack 面是 **cyber**(命令/文件/网络/凭证/沙箱)。现有 attack 集(shell-injection/credential-exfil/sandbox-escape/supply-chain)本就是 cyber,深化它对标 Anthropic cyber-risk taxonomy,合规零风险。

### 方案
扩 cyber 到 6 子类,每子类 5-8 条:
1. privilege escalation(权限提升)
2. lateral movement(横向移动)
3. persistence(持久化)
4. data exfiltration(数据外传)
5. credential theft(凭证窃取)
6. supply chain(供应链)

### 改文件
`eval/promptfoo/attacks.yaml`(+30)、`eval/promptfoo/defense_matrix.yaml`(登记 6 子类 layer + severity)、`eval/promptfoo/tools/generate_attacks.py`(动态生成 cyber 子类)

### 验收
- attacks.yaml 新增 ≥30 条(6 子类各 5+)
- defense_matrix.yaml 登记无 `UnknownCategoryError`
- report 分子类展示 ASR

### 不做
- CBRN / autonomy / persuasion(概念错位:不贴 coding agent)

## §7 #14 Locomo 桥接

### 现状
`eval/locomo/` 已是**完整子系统**:`dataset.py` / `evaluator.py`(+v3)/ `metrics.py` / `report.py` / `runner.py`(带 resume,`.checkpoint.json`)/ `policy_local.yaml` + 完整测试套件(test_evaluator/test_metrics/test_report/test_runner_resume/test_dataset_session_index/test_maintenance_locomo)。**不是孤岛,是没并进主 report。**

### 方案
> **plan 前置**:先读 `eval/locomo/metrics.py` + `evaluator.py`(v3)确定现有指标,scope 可能再缩。

- 桥接 locomo report 进 `unified-report.md`
- 指标对齐命名:`memory_recall@1`、`memory_recall^5`、`PII_leak_rate`
- 复用主 wrapper/judge/report 链路(或 locomo 自带 runner 出报告后 adapter 合并)

### 改文件
`eval/locomo/report.py`(或新增 adapter)、`eval/promptfoo/tools/run_eval.py`(`_unified` 合并 locomo 输出)、`report_to_md.py`(memory 维度段)

### 验收
- locomo 子集跑通 → unified-report 含 memory 段(recall@1 / recall^5 / PII_leak_rate)

### 不做
- locomo 全量(子集够)
- 跨 session 记忆(看现状定)

## §8 #9 Write-up

### 方案
1. `docs/eval-methodology.md`:7 项设计 + 第 6 章/AgentGuide 概念映射 + 自家方法论(per-layer ASR / Pass^k / trajectory / multi-judge / gold-set / cyber taxonomy / locomo)
2. `CLAUDE.md` 的「Eval / red-team」一节更新(eval-v2 新增能力)
3. 1 篇 Zhihu(对外,秋招可见)

### 验收
- eval-methodology.md 覆盖 7 项 + 概念映射
- Zhihu 可发

### 不做
- 英文版

## §9 依赖顺序(plan phase 划分建议)

```
Phase 1: #2 (wrapper + JSONL + HTML 可视化)          — 地基,#1/#14 依赖
Phase 2: #1 (Pass^k report) ∥ #3 (双源 judge)        — 并行,依赖 #2
Phase 3: #4 (gold-set,依赖 #3) ∥ #7 (cyber 数据)    — 并行
Phase 4: #14 (locomo 桥接) + #9 (文档)               — 收尾
```

## §10 不做(YAGNI 汇总)
- #15 公开 benchmark(MMLU/HumanEval 概念错位,留 backlog)
- CBRN / autonomy / persuasion(coding agent 不贴)
- 全量 Pass^k ×5(成本)
- 3+ judge 源(架构留口,先 2 源)
- 多人盲标(单人 + 抽样复核)
- trajectory 可视化并进 web(留后续)
- 英文 write-up

## §11 风险与缓解
- **成本**:DeepSeek/MiMo 跑 turn 便宜;critical×5 + 双 judge 控调用数;预算 ~$10-20
- **MiMo 同源 agent**:两 judge 之间异源(cloud vs MiMo)即防 Goodhart;残余风险由 cloud 兜底;后续加 GPT-4o 第三源
- **locomo 桥接复杂度**:plan 前置读 evaluator_v3/metrics,可能 scope 再缩
- **gold-set 标注瓶颈**:单人 50 标 ~8-12h;失败驱动收集自动化,仅人工标
- **n=5 统计力**:Wilson CI 在 n=5 仍宽,report 诚实标注 CI 宽度,不夸大

## §12 与现有 eval 的兼容性
- `attacks.yaml` / `defense_matrix.yaml` / `judges/attack_held_ground.txt` / wrapper deny-allow 双模式 / self-mod 守卫 / `severity_gate` / `compute_asr_by_layer` —— **全部保留**,eval-v2 只增不改坏
- `run_eval.py unified` 入口保留向后兼容;新增能力通过 config flag / report 扩展段接入
- 现有 ~1468 测试 baseline 不回归(新代码带自己的 tests/)
