# 运行手册与故障排查 (Runbook)

## 1. 快速启动

前置: 安装 Docker 与 Docker Compose v2(需可访问 Docker Hub 与 Maven Central 的网络环境)。

```bash
cp .env.example .env            # 按需修改密钥(APP_JWT_SECRET / APP_CRYPTO_KEY)
./scripts/start.sh              # 一键:构建镜像→启动 MySQL/Redis/RabbitMQ/App
./scripts/healthcheck.sh        # 健康检查(含依赖探针)
```

启动后:
- 应用: http://localhost:8080  (`GET /api/health`)
- 前端(可选): `cd frontend && npm install && npm run dev` → http://localhost:5173(代理 /api→8080)
- RabbitMQ 控制台: http://localhost:15672 (guest/guest,需在 compose 中显式启用)
- 演示账号(密码均为 `123456`,启动时 DataInitializer 重置):
  - 用户: zhangsan / lisi
  - 商家: m_hanbao(快乐汉堡) / m_chuan(川味小馆)
  - 骑手: rider1 / rider2
  - 管理: admin

## 2. 配置说明

| 变量 | 默认(dev) | 说明 |
|------|-----------|------|
| APP_JWT_SECRET | dev-only-... | JWT HS256 密钥,生产必须外部注入,缺失启动失败 |
| APP_CRYPTO_KEY | 32 字节占位 | 手机号 AES-128-GCM 密钥,生产必须外部注入 |
| MYSQL_HOST/PORT/DB/USER/PASSWORD | mysql/3306/campus_delivery/campus/campus123 | DB 连接 |
| REDIS_HOST/PORT/PASSWORD | redis/6379/(空) | Redis 连接 |
| RABBIT_HOST/PORT/USER/PASSWORD | rabbitmq/5672/campus/campus123 | RabbitMQ 连接 |
| app.adapter.payment|sms|delivery | mock | 模拟适配器开关 |
| app.seed.demo-password-reset | true | 启动时把演示账号密码重置为 123456(幂等) |

## 3. 数据库迁移与种子

- MySQL 容器挂载 `./db/init:/docker-entrypoint-initdb.d`(仅首次空库执行 01-schema.sql、02-seed.sql)。
- 后续迁移: 将新脚本按序号放入 `db/init` 会因容器已初始化不执行;生产迁移建议引入 Flyway
  (未覆盖项,当前交付为 init 脚本 + 手动 `mysql < 03-xxx.sql` 说明)。
- 重建: `docker compose down -v && ./scripts/start.sh`(会清空数据)。

## 4. 健康检查

`GET /api/health` 返回:
```json
{"status":"UP","components":{"db":"UP","redis":"UP","rabbit":"UP"},"version":"1.0.0","time":"..."}
```
`scripts/healthcheck.sh` 轮询该接口并在组件 DOWN 时给出修复提示。

## 5. 故障排查

| 症状 | 排查 | 修复 |
|------|------|------|
| App 启动失败: DB 连接拒绝 | `docker compose ps`;`docker compose logs mysql` | 等 MySQL 就绪后重启 app;检查 .env 密码 |
| Redis 连接失败 | `docker compose logs redis` | 检查 REDIS_HOST/PASSWORD |
| 消息不消费/通知缺失 | RabbitMQ 控制台看 queue.notify.* 积压与 x-death | 消费端异常看 app 日志;DLQ 消息人工处理后清除 |
| 支付回调幂等生效验证 | 重复 POST /api/payment/callback 同 trade_no | 第二次返回同样成功,payment_record 仅一条 |
| 库存超卖验证 | 并发下单同菜品 | dish.stock 不出现负数,300103 正常返回 |
| 手机号脱敏 | GET /api/auth/me | 返回 138****1234 格式 |
| 缓存未更新 | 修改菜品价格后列表仍是旧价 | 检查是否走了 delete-on-write(改价接口必须调用缓存失效) |
| 演示账号登录失败 | 是否执行过 DataInitializer(首次启动) | 重启 app 一次或手动执行 seed 中密码重置逻辑 |

## 6. 一键脚本
- `scripts/start.sh`: 检查 docker → compose up -d mysql redis rabbitmq → 等健康 → 构建并启动 app。
- `scripts/stop.sh`: compose down(保留数据)。
- `scripts/healthcheck.sh`: 健康检查 + 简要诊断。
- `scripts/api-smoke.sh`: 核心链路冒烟(注册→登录→浏览→加购→下单→支付→配送→评价)。

## 7. 测试与静态检查(在具备 JDK17+Maven 的环境)
```bash
mvn -q -DskipTests=false test          # 单元 + 集成测试(H2,无需容器)
mvn -q -pl campus-web -am package      # 打包
python3 tools/static-analysis/layer_check.py   # 依赖方向静态检查(沙箱内可运行)
python3 tools/static-analysis/sql_audit.py
node scripts/verify-api-contract.cjs   # 后端控制器 / 前端 api / docs/api.md 三方契约核对(沙箱内可运行)
cd frontend && npm install && npm test # 前端关键交互测试
```

## 8. 沙箱环境说明(如实记录)
本仓库开发环境无 JDK/Maven/Docker 且外网受限(npm registry 不可达,DNS 解析失败),故:
- **已在本环境运行并验证**:
  - 静态检查(layer/sql/json/结构)、脚本语法检查;
  - Node 参考逻辑测试(node:test): `logic-harness/*.test.js`(24 项通过);
  - 前端离线验证(见 `frontend/README.md`): `npm test` / `npm run test:ui` 均执行
    `node --test tests/`(17 项纯逻辑单测通过)、`node scripts/verify-syntax.cjs`(40/40 js/jsx
    babel 解析通过)、`node scripts/verify-static.cjs`(相对 import 全部可解析)、
    `node scripts/verify-api-contract.cjs`(后端/前端/文档三方契约一致);
  - `npm run build`(vite build)已在本环境实测通过(产物 `frontend/dist/`,35s);
  - 测试运行器已统一为 node:test(移除未使用的 vitest 依赖;`test:ui` 为 `npm test` 别名)。
- **未在本环境运行(需外部环境复现)**: `mvn test/package`、Docker Compose 健康链路
  (依赖 JDK/Maven/Docker 运行时,本沙箱缺失)。
详见 `observability/environment-audit.json`、`observability/phase2-frontend.jsonl`、
`observability/phase3-integration.jsonl` 与最终报告。
