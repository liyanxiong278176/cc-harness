# 代码规范

## 1. 包结构与依赖方向
- 模块: `campus-common` / `campus-dao` / `campus-service` / `campus-web`。
- 依赖: `web → service → {dao, mq} → common`;`dao/mq` 不得依赖 `service/web`;`common` 无业务依赖。
- 包名: `com.campus.common|dao|service|web`。
- 分层文件命名: Controller 后缀 `Controller`;Service 接口 `XxxService` + 实现 `XxxServiceImpl`(置于 impl 子包);
  Mapper 继承 `BaseMapper<T>`;DTO/请求 `XxxRequest`、响应 `XxxVO`。

## 2. Java 编码规范
- JDK 17;禁止使用原始类型、未捕获泛型警告;使用 `var` 仅限局部且语义明确。
- 金额一律 `BigDecimal`,禁止 double 参与金额计算;金额工具 `MoneyUtils`(半向上取整,2 位)。
- 时间使用 `LocalDateTime`;ID 自增 BIGINT;业务单号 `IdUtils.orderNo()`。
- 常量: 状态/角色/消息键定义在 `com.campus.common.constant`;禁止魔法字符串散落。
- 异常: 业务异常抛 `BizException(ResultCode)`;禁止 `printStackTrace`,统一日志。
- 事务: 写操作 `@Transactional(rollbackFor = Exception.class)` 置于 Service 实现类公开方法;
  MQ 消费方法自成一事务(传播 REQUIRES_NEW 由消费框架控制)。
- 乐观锁: 实体 `@Version`;更新用 `updateById`(MP 自动带 version 条件)。
- 逻辑删除: 实体 `@TableLogic`。
- 参数校验: 入参对象用 JSR-380 注解;Controller 不写业务判断。

## 3. SQL 规范
- 一律 MyBatis-Plus 或 XML Mapper 参数化(`#{}`);**禁止 `${}`** 与字符串拼接(静态检查强制)。
- 表名/字段蛇形,索引命名 `uk_`/`idx_` 前缀;所有表含审计字段(见 schema)。
- 查询必须命中索引(主键/唯一键/组合索引),分页用 MP 分页插件。

## 4. 前端规范
- React 函数组件 + Hooks;状态管理 AuthContext + 局部 state;请求封装 `api/http.js`(统一错误提示)。
- 组件粒度: 页面在 `pages/*`,通用组件在 `components/*`。
- 用户端/商家端路由分区:`/user/*` 与 `/merchant/*`,`RequireAuth` 包裹,按角色重定向。
- 金额展示统一 `formatMoney`;时间展示 `dayjs`。

## 5. 测试规范
- 单测: 纯逻辑(状态机/金额/幂等/防超卖),JUnit5 + Mockito,不依赖容器。
- 集成: MockMvc + H2(MySQL 模式)profile `test`,Redis/Rabbit 用 MockBean。
- 前端: Vitest + Testing Library 关键交互(登录/加购/结算)。
- 命名: `XxxTest`(单测)/ `XxxIntegrationTest`(集成)。

## 6. 静态检查工具(本仓库 `tools/static-analysis/`)
| 工具 | 检查项 | 运行 |
|------|--------|------|
| layer_check.py | 模块依赖方向 | `python3 tools/static-analysis/layer_check.py` |
| sql_audit.py | `${}` 禁用、表/字段引用与 schema 一致性 | `python3 tools/static-analysis/sql_audit.py` |
| json_check.py | 全部 JSON 可解析 | `python3 tools/static-analysis/json_check.py` |
| struct_check.py | 必需文件/模块结构存在性 | `python3 tools/static-analysis/struct_check.py` |
