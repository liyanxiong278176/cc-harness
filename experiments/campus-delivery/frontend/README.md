# campus-delivery 前端(React + Ant Design)

校园外卖前端,与后端 Spring Boot(`/api` 统一响应 `{code,message,data}`)集成。
同一代码仓内按路由分区:用户端 `/`、商家端 `/merchant`、骑手端 `/rider`。

## 技术栈
- React 18 + Ant Design 5 + React Router 6
- Vite 5(dev 代理 `/api` → 后端 `8080`)
- 状态:全局登录态用 `AuthContext`;组件内局部 state
- 请求封装:`src/api/http.js`(自动携带 JWT,`code===0` 解包,401 跳登录)

## 目录结构
```
frontend/
  package.json
  vite.config.js          # /api 代理
  index.html
  .env.example            # VITE_API_BASE / VITE_BACKEND_URL
  scripts/
    verify-syntax.cjs     # babel 解析全部 js/jsx(离线可跑)
    verify-static.cjs     # 相对 import 可解析校验(离线可跑)
  tests/                  # node:test 纯逻辑单测(离线可跑,无第三方依赖)
  src/
    api/                  # auth/user/merchants/cart/orders/payment/rider/merchantAdmin/health
    store/AuthContext.jsx # 登录态
    components/RequireAuth.jsx
    layouts/              # UserLayout / MerchantLayout / RiderLayout
    pages/                # 登录注册 + user/* + merchant/* + rider/*
    utils/                # format / cart 纯函数
```

## 运行
```bash
cd frontend
npm install
npm run dev        # http://localhost:5173,自动代理 /api -> localhost:8080
npm run build      # 产物 dist/
npm run preview
```

## 离线验证(当前沙箱无 npm registry 可达性时使用)
```bash
npm test                      # 或 npm run test:ui —— 统一 node:test 运行器(17 项纯逻辑单测)
npm run build                 # vite build(已在本环境实测通过)
node scripts/verify-syntax.cjs # 全部 js/jsx babel 语法解析
node scripts/verify-static.cjs # 相对 import 一致性
# 后端/前端/文档三方接口契约核对(仓库根目录执行):
node scripts/verify-api-contract.cjs
```
> 说明:测试运行器统一为 Node 内置 `node:test`(零外部依赖,离线可用);
> `npm run test:ui` 是 `npm test` 的别名(当前套件均为纯逻辑测试,无需 vitest/jsdom)。

## 演示账号(密码均 123456)
| 角色 | 用户名 | 入口 |
| --- | --- | --- |
| 用户 | zhangsan | `/` |
| 商家 | m_hanbao | `/merchant` |
| 骑手 | rider1 | `/rider` |

## 与后端接口对照
各 API 模块与 `docs/api.md` 章节一一对应:
`auth.js`→§1,`user.js`→§2,`merchants.js`→§3,`cart.js`→§4,`orders.js`→§5,`payment.js`→§6,`rider.js`→§7,`merchantAdmin.js`→§8。
