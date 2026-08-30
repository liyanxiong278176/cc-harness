# API 规约(实现权威版)

> Base URL: `/api`。响应统一 `Result<T>`:`{"code":0,"message":"success","data":...}`;分页用 `PageResult<T>`:`{"records":[],"total":n,"size":n,"current":n}`。
> 鉴权: `Authorization: Bearer <jwt>`。角色: `USER / MERCHANT / RIDER / ADMIN`(`@RequireRole`)。
> 错误码: `code!=0` 即为业务错误,见 `ResultCode`(本文档 §9 与 campus-common 源码为权威)。

## 0. 健康检查 /health(公开)
| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| GET | /health | 公开 | 组件探针:返回 `{status: UP\|DOWN, components:{db,redis,rabbit}, version, time}`;单组件失败仅标记 DOWN,不返回业务错误(详见 docs/operations.md) |

## 1. 认证 /auth

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | /auth/register | 公开 | body `{username,password,nickname,phone}`;role 固定 USER;返回 `{token,user}` |
| POST | /auth/login | 公开 | body `{username,password}`;返回 `{token,user}` |
| GET | /auth/me | 已登录 | 当前用户(手机号脱敏) |
| PUT | /auth/password | 已登录 | body `{oldPassword,newPassword}` |

## 2. 用户 /user

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| PUT | /user/profile | USER | body `{nickname,avatar,phone}`(phone 入库加密) |
| GET | /user/addresses | USER | 地址列表(手机号脱敏) |
| POST | /user/addresses | USER | body `{receiverName,receiverPhone,campusZone,detail,isDefault}` |
| PUT | /user/addresses/{id} | USER | 更新(校验归属) |
| DELETE | /user/addresses/{id} | USER | 删除(逻辑删) |
| GET | /user/coupons | USER | query `status=UNUSED/USED/EXPIRED`(缺省全部) |
| POST | /user/coupons/{couponId}/receive | USER | 领取优惠券 |
| GET | /user/notifications | USER | 分页通知 |
| PUT | /user/notifications/{id}/read | USER | 标记已读 |
| PUT | /user/notifications/read-all | USER | 全部已读 |
| GET | /user/notifications/unread-count | USER | 未读数 |

## 3. 商家浏览 /merchants

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| GET | /merchants | 公开/登录 | query `zone,page,size`;营业中优先 |
| GET | /merchants/{id} | 公开/登录 | 商家详情 |
| GET | /merchants/{id}/menu | 公开/登录 | 分类+上架菜品(Redis 缓存) |

## 4. 购物车 /cart(USER)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /cart | 按商家分组 + 合计 |
| POST | /cart/items | body `{dishId,quantity}` |
| PUT | /cart/items/{dishId} | body `{quantity}` |
| PUT | /cart/items/{dishId}/check | body `{checked}` |
| DELETE | /cart/items/{dishId} | 删除行 |
| DELETE | /cart | 清空(仅已勾选) |

## 5. 订单 /orders(USER)

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /orders/checkout | body `{merchantId,addressId,couponId,remark}`;使用该商家已勾选购物车行;返回 `{orderNo}` |
| GET | /orders | query `status,page,size` |
| GET | /orders/{orderNo} | 详情(含明细/状态时间) |
| POST | /orders/{orderNo}/cancel | body `{reason}`;仅 CREATED |
| POST | /orders/{orderNo}/pay | body `{channel}`;创建模拟支付,返回 `{paymentNo,payParams}` |
| POST | /orders/{orderNo}/refund | body `{reason}`;仅 PAID/PREPARING/DELIVERING |
| POST | /orders/{orderNo}/review | body `{rating(1-5),content,images}`;仅 COMPLETED;同一订单仅可评一次 |
| GET | /orders/{orderNo}/track | 订单+支付+配送状态跟踪 |

## 6. 模拟支付 /payment

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | /payment/mock/notify | 模拟渠道调用(服务内部/可手动) | body `{orderNo,success,channel}`;**幂等**(trade_no 唯一 + dedup 键 + FOR UPDATE) |
| GET | /payment/mock/notify | 手动测试 | query `orderNo,success` |

## 7. 骑手 /rider(RIDER)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /rider/tasks | query `status`;我的配送任务 |
| GET | /rider/tasks/available | 待接单池 |
| POST | /rider/tasks/{id}/accept | 抢单(条件更新防双抢) |
| POST | /rider/tasks/{id}/pickup | 取餐 |
| POST | /rider/tasks/{id}/deliver | 送达 |

## 8. 商家管理 /merchant(MERCHANT)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /merchant/dashboard | 今日订单/营业额/待处理 |
| GET/PUT | /merchant/profile | 商家资料 |
| PUT | /merchant/business-status | body `{isOpen}` |
| GET | /merchant/categories | 分类列表 |
| POST | /merchant/categories | body `{name,sortOrder}` |
| PUT | /merchant/categories/{id} | 更新 |
| DELETE | /merchant/categories/{id} | 删除(有菜品则拒绝) |
| GET | /merchant/dishes | query `categoryId,status,page,size` |
| POST | /merchant/dishes | body `{categoryId,name,description,image,price,originalPrice,stock}` |
| PUT | /merchant/dishes/{id} | 更新基本信息 |
| PUT | /merchant/dishes/{id}/stock | body `{stock}`(仅直接设置,RESTOCK 流水) |
| PUT | /merchant/dishes/{id}/status | body `{status}`(1 上架/0 下架) |
| GET | /merchant/orders | query `status,page,size` |
| POST | /merchant/orders/{orderNo}/accept | 接单(PAID→PREPARING) |
| POST | /merchant/orders/{orderNo}/ready | 出餐(配送派单,触发 dispatch) |
| GET | /merchant/reviews | 评价列表 |
| POST | /merchant/reviews/{id}/reply | body `{reply}` |
| GET | /merchant/refunds | 退款申请列表 |
| POST | /merchant/refunds/{id}/approve | 同意退款(走模拟退款,恢复库存) |
| POST | /merchant/refunds/{id}/reject | body `{reason}` 拒绝 |

## 9. 错误码(节选,权威见 ResultCode.java)

`0` 成功;通用 `40000` 参数、`40101` 未登录、`40301` 无权限、`40401` 不存在、`40901` 冲突、`42901` 限流、`50000` 内部、`50001` DB、`50002` 外部模拟服务。
业务域: 用户 `1001xx`、商家 `2001xx`、菜品 `3001xx`、购物车/订单 `4001xx`、支付/退款 `5001xx`、配送 `6001xx`、评价 `7001xx`、通知 `8001xx`。

## 10. 约定

- 金额字段均为数字(元,2 位小数)。
- 列表接口默认分页 `page=1,size=10,size<=100`。
- 状态枚举值见 `Constants`(OrderStatus/DeliveryStatus/PayStatus/RefundStatus/UserCouponStatus/NotificationType)。
- 登录/注册接口放行;其余需 `Authorization`。
