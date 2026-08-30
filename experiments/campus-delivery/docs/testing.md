# 测试策略与清单

> 目标: 单元测试 + 核心接口集成测试 + 前端关键交互测试,三层齐全,测试不需要 Docker/真实中间件。

## 1. 测试分层

| 层 | 技术 | 运行前提 | 存放位置 |
|----|------|----------|----------|
| 纯逻辑单元测试 | JUnit 5 + Mockito | JDK + Maven(离线仓库) | 各模块 src/test |
| Web 集成测试 | @SpringBootTest + MockMvc + H2(MySQL 模式) | 同上;Redis/Rabbit 用 MockBean 或 LocalCacheClient | campus-web/src/test |
| 前端逻辑单测 | node:test(无依赖,可真跑) | Node 20 | frontend/src/utils/*.test.js |
| 前端组件测试 | Vitest + Testing Library | npm install | frontend/src/**/*.test.jsx |
| 逻辑参考测试 | node:test(无依赖,可真跑) | Node 20 | logic-harness/*.test.js |
| 静态检查 | python3 / node | 无 | scripts/ |

## 2. 单元测试清单(campus-service / campus-common)

- `AesUtilsTest`: AES-GCM 加解密 roundtrip、空串、错误密钥。
- `MaskUtilsTest`: 手机号/姓名脱敏边界。
- `MoneyUtilsTest`: 金额精度、折扣计算、比较。
- `JwtUtilsTest`: 生成/解析/过期/篡改签名。
- `OrderStateMachineTest`: 合法/非法迁移矩阵。
- `CouponCalculatorTest`: 满减/折扣券、门槛、上限。
- `CartPriceCalculatorTest`: 金额合计、分店分组。

## 3. 集成测试清单(campus-web)

- `AuthFlowIT`: 注册→登录→携带 JWT 访问受保护接口→无 Token 401→错误角色 403。
- `OrderCheckoutIT`: 加购→结算(库存充足)→库存不足报错→订单落库→购物车清空。
- `PaymentCallbackIT`: 模拟支付回调两次,第二次幂等成功且只生成一条支付流水/一次状态流转。
- `CouponFlowIT`: 领券→结算抵扣→重复使用被拒。
- `ReviewFlowIT`: 未完成订单不可评价→完成后评价→商家评分聚合。
- `NotificationIT`: 订单事件 → 通知落库(uk_dedup 幂等)。

集成测试使用 `application-test.yml`: H2 MODE=MySQL、Flyway 关闭、LocalCacheClient(内存)、RabbitTemplate 全部 MockBean、Rabbit 监听器 auto-startup=false。

## 4. 前端测试

- `utils/cart.test.js`(node:test,可真跑): 加减数量、库存上限、勾选、金额合计(分)。
- `utils/price.test.js`: 分↔元转换。
- `utils/auth.test.js`: token 存取/过期判断。
- `components/LoginForm.test.jsx`(Vitest): 表单校验与提交回调。
- `pages/Checkout.test.jsx`(Vitest): 金额展示与提交。

## 5. 逻辑参考测试(logic-harness,Node,可真跑)

与后端文档语义一致的可执行参考实现,作为"无 JDK 环境下的真实证据":

- `stock-deduct.test.js`: 并发扣减不超卖(模拟条件更新)。
- `price-calc.test.js`: 满减/折扣计算。
- `order-state-machine.test.js`: 订单/配送状态迁移合法性。
- `payment-idempotency.test.js`: 重复回调去重。
- `coupon.test.js`: 券领取/使用/过期。

## 6. 运行方式

```bash
# Java 全部测试(需 JDK17+Maven)
mvn -q test
# 仅某模块
mvn -q -pl campus-service -am test
# 前端逻辑单测(无依赖,本沙箱可运行)
node --test frontend/src/utils/
# 逻辑参考测试(无依赖,本沙箱可运行)
node --test logic-harness/
# 静态检查(本沙箱可运行)
python3 scripts/layer_check.py && python3 scripts/sql_audit.py && node scripts/verify-static.js
```
