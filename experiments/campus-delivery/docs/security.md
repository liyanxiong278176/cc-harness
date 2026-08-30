# 安全设计

## 1. 认证与授权
- 登录/注册 → 签发 JWT(HS256,密钥 `APP_JWT_SECRET` 来自环境变量,默认仅 dev);
  claims: `uid, username, role`,有效期 12h。
- `JwtAuthInterceptor`: 除白名单(`/api/auth/**, /api/health, /api/payment/callback` 签名校验)外全部校验;
  ThreadLocal `UserContext` 注入当前用户;响应头 `X-User-Id` 供审计。
- `@RequireRole(USER/MERCHANT/RIDER/ADMIN)`: 角色不符抛 40301;商家接口额外校验
  `MerchantEmployeeService.assertMerchantOwner(uid, merchantId)`。
- 密码: BCrypt(`spring-security-crypto`),禁止明文入库/出参/日志。

## 2. 敏感数据
- 手机号: 入库 AES-128-GCM 加密(`AesUtils`,`APP_CRYPTO_KEY` 来自环境变量,compose/.env 注入,32 字节),
  出参 `MaskUtils` 脱敏为 `138****1234`;地址快照同理。
- 日志脱敏: `LogMasker` 拦截并替换手机号/密码/token 字段;operation_log 存脱敏后参数。
- 密钥管理: 密钥不入代码不入文档;`docs/operations.md` 说明从环境变量注入;本仓库仅存 `.env.example` 占位。

## 3. 接口安全
- 参数校验: `@Valid` + JSR-380(`@NotBlank/@Size/@DecimalMin/@Pattern` 等),统一 40000。
- SQL 注入: 全部 MyBatis-Plus 参数化(`#{}`),Mapper XML 禁止 `${}`(sql_audit.py 静态检查强制)。
- 越权防护: 资源归属校验(订单/地址/券/购物车均校验 userId);商家资源校验所属店铺。
- 限流: 简单滑动窗口限流器(`RateLimiter`)用于登录/领取优惠券接口,超限 42901。

## 4. 传输与部署安全
- 生产 profile 强制 `server.port=8080`;JWT/加密密钥必须由外部环境注入(缺失则启动失败,`fail-fast`)。
- Docker: 应用以非 root 用户运行;健康检查;MySQL 初始化卷只读权限;网络按 compose 隔离。

## 5. 审计
- `OperationLogAspect`: 拦截 `@OperationLog` 注解方法,记录 用户/操作/URI/IP/脱敏参数/耗时/结果。
- 审计字段: 每表 `created_by/updated_by/created_at/updated_at/version/deleted`(MyBatis-Plus MetaObjectHandler)。
- 敏感操作(登录失败/退款/改库存)强制记 operation_log。

## 6. 已知边界(如实声明)
- 未引入 Spring Security 全量(仅 crypto),令牌黑名单未实现(登出为前端清除 token);如需服务端登出,
  可加 Redis token 黑名单——作为未覆盖项记录于最终报告。
- 支付回调"验签"为模拟: Mock 网关回调带 `sign=HMAC-SHA256(secret, payload)`,回调接口校验签名。
