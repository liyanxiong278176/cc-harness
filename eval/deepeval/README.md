# DeepEval 调研记录

## 结论:**选型通过,目前不启用**

DeepEval 4.0.6 是 pytest 形式的 LLM 输出质量评测库,50+ research-backed metric。

## 跑通过验证(已撤回)

曾经在 `D:\agent_learning\cc-harness\eval\deepeval\` 完整配置过:

- `test_capability_metrics.py` —— 5 个测试(Faithfulness × 2, Answer Relevancy × 2, Contextual Precision × 1)
- `conftest.py` —— `judge_model` fixture,把 deepeval 默认读 OpenAI 的 judge 改成 DeepSeek
- `pytest.ini` —— 独立 pytest 配置(asyncio_mode=auto)

验证:
```bash
.venv/Scripts/python.exe -m pytest eval/deepeval/ -v
# 5 passed in 32.19s
```

## 主要价值

1. **pytest 原生集成** —— 跟项目现有 217 个测试同一框架,跑出来报告格式统一
2. **50+ metric** —— Faithfulness / Answer Relevancy / Contextual Precision / Hallucination / Toxicity / Bias ...
3. **GEval 自定义** —— 用自然语言描述 metric,LLM 当 judge
4. **0 元** —— Apache 2.0 OSS,本地跑,数据不出机器

## 当时不适用的原因

- **metric 调 LLM 当 judge,跟 promptfoo 同样的局限**:不走 cc-harness ReAct loop
- **DeepEval 4.x 没有全局 model 配置** —— 每个 metric 显式传 `model=judge_model`,略繁琐
- **GEval 校准怪** —— LLM 说对但给 0.1 分,部分 case 改为确定性断言更稳
- **DeepSeekModel 默认读 `DEEPSEEK_API_KEY`**,需要显式传 `api_key=`

## 何时再启用

- 需要 CI 质量门禁(每次 commit 跑 metric)
- 想做 A/B 对比(换 prompt / 换 model 前后跑同一组 metric)
- 用户报告输出质量问题,需要量化

## 重启命令(未来)

```bash
.venv/Scripts/python.exe -m pip install deepeval
# 重新写 conftest.py + test_capability_metrics.py(参考 git history)
.venv/Scripts/python.exe -m pytest eval/deepeval/ -v
```

## 坑点备忘(避免重蹈覆辙)

```python
# 1) 不要写 deepeval.model = ...,这个属性不存在
#    必须每个 metric 显式传 model=judge_model

# 2) DeepSeekModel 默认读 DEEPSEEK_API_KEY
#    显式传 api_key=os.getenv("OPENAI_API_KEY")

# 3) GEval criteria 单步比多步稳,threshold 用 0.5 起步

# 4) ContextualPrecision / ContextualRecall 需要 expected_output
#    没 ground truth 时改用 AnswerRelevancy

# 5) 提示词里有中文时,设 PYTHONIOENCODING=utf-8,避免 Windows GBK 崩
```

## 参考

- https://docs.confident-ai.com/docs/metrics-introduction
- https://docs.confident-ai.com/docs/metrics-faithfulness
