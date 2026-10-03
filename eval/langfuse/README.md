# Langfuse 配置说明(暂未启用)

## 当前状态
**本目录只保留 README 文档,不包含任何运行时配置。**

> Langfuse v3 自托管需要 Postgres + ClickHouse + Redis + Langfuse web 4 个容器协同。
> 在 Windows Docker Desktop 上 ClickHouse 的 ReplicatedMergeTree 迁移会因为
> 缺少 Zookeeper 报错。要正确启用,需要做以下两件事之一:
>
> - 配 Zookeeper(单节点 ClickHouse 不需要,3 节点集群才需要)
> - 找 langfuse 3.x 提供的 `CLICKHOUSE_CLUSTER_NAME` 等开关强制单节点模式
>
> 目前**没有时间/动力继续折腾**,先全部回退。

## 为什么把 Langfuse 放进来考虑

| 角度 | 评估 |
|---|---|
| 功能 | 跟 Arize Phoenix 重叠(都是 OTel 自托管 trace 平台) |
| 集成 | `from langfuse.openai import AsyncOpenAI` 一行接入 cc_harness/llm.py |
| 适合度 | 适合长链路 agent debugging,UI 体验比 Phoenix 好 |
| 阻塞 | v3 部署复杂(Postgres + ClickHouse + Redis),v2 已被官方弃用 |

## 备选方案(暂不动)

1. **Arize Phoenix**: OTel-native,Python `pip install arize-phoenix`,`phoenix serve` 一行起。比 Langfuse 轻很多,集成成本相近。
2. **回到 OTel-only**: 接 Jaeger / Tempo / SigNoz 这类通用 trace 后端。cc_harness 不用专用的 LLM trace 平台,通用 OTel 也够用。

## 如果以后要启用 Langfuse

参考 https://langfuse.com/self-hosting 看最新 docker-compose。v3 完整部署包括:
- postgres (元数据)
- clickhouse (trace 数据,需要 Zookeeper 或关闭 cluster 模式)
- redis (队列)
- langfuse/langfuse:3 (web + worker)

## 决策记录(2026/06)

- 选 Langfuse 而非 Braintrust: 开源 + 自托管 + 0 元
- 选 Langfuse 而非 Phoenix: UI 体验好,transcript view 适合 agent
- 最终不用 Langfuse: v3 部署成本 > 价值,先靠 promptfoo + DeepEval 顶上
