#!/usr/bin/env bash
# 核心业务链路冒烟测试(需应用已启动,依赖 python3 解析 JSON)
# 链路: 注册/登录 -> 商家资料(认证) -> 加购 -> 结算 -> 支付(模拟回调) -> 商家接单/出餐 -> 骑手接单/取餐/送达 -> 评价
# 规则: 商家 ID 必须取自 M_TOKEN 认证的 GET /merchant/profile(公开列表可能属于别的商家);
#       所有变更调用断言 Result.code==0(HTTP 200 也可能携带业务错误码,必须显式检查);
#       配送任务优先取骑手自有任务(/rider/tasks),空则回退开放池(/rider/tasks/available)。
set -euo pipefail
BASE=${BASE:-http://localhost:8080/api}

step() { echo; echo "== $* =="; }
json_get() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }
# 断言 HTTP 200 响应体的业务码为 0;否则打印响应并失败退出(捕获 HTTP 200 携带业务错误的场景)
check_code() {
  local body code
  body=$(cat)
  code=$(echo "$body" | python3 -c "import sys,json;print(json.load(sys.stdin).get('code',-999))" 2>/dev/null || echo -999)
  if [ "$code" != "0" ]; then
    echo "[campus] 错误: 业务失败 code=$code 响应=$body" >&2
    exit 1
  fi
}

step "1. 用户注册+登录"
R=$(curl -sf -X POST "$BASE/auth/register" -H 'Content-Type: application/json' \
  -d '{"username":"smoke_'$RANDOM'","password":"123456","phone":"13900001111","role":"USER"}' || true)
if echo "$R" | json_get "['code']" 2>/dev/null | grep -q '^0$'; then
  U_TOKEN=$(echo "$R" | json_get "['data']['token']")
else
  echo "  (注册失败或用户已存在,回退登录)"
  R=$(curl -sf -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d '{"username":"zhangsan","password":"123456"}' || true)
  echo "$R" | check_code
  U_TOKEN=$(echo "$R" | json_get "['data']['token']")
fi
echo "  user token ok: ${U_TOKEN:0:12}..."

step "2. 商家/骑手登录"
MR=$(curl -sf -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"m_hanbao","password":"123456"}')
echo "$MR" | check_code
M_TOKEN=$(echo "$MR" | json_get "['data']['token']")
RR=$(curl -sf -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"rider1","password":"123456"}')
echo "$RR" | check_code
R_TOKEN=$(echo "$RR" | json_get "['data']['token']")
echo "  merchant/rider token ok"

step "3. 商家资料(认证)+菜单"
# MID 必须来自 M_TOKEN 认证的商家资料;公开列表可能是别的商家,会导致商家操作业务失败
P=$(curl -sf "$BASE/merchant/profile" -H "Authorization: Bearer $M_TOKEN")
echo "$P" | check_code
MID=$(echo "$P" | json_get "['data']['id']")
echo "  merchantId(profile)=$MID"
# 公开列表仅作契约覆盖,不用于商家操作
curl -sf "$BASE/merchants?page=1&size=5" | json_get "['data']['records'][0]['id']" >/dev/null 2>&1 || true
# 契约: GET /merchants/{id}/menu -> data.merchant + data.categories[*].dishes[*]
# 展平所有分类取首个上架菜品(首个分类可能为空,不依赖分类顺序)
DISH_ID=$(curl -sf "$BASE/merchants/$MID/menu" | python3 -c "
import sys, json
d = json.load(sys.stdin)['data']
dishes = [x for c in (d.get('categories') or []) for x in (c.get('dishes') or []) if x.get('id')]
print(dishes[0]['id'] if dishes else '')
")
if [ -z "$DISH_ID" ]; then
  echo "[campus] 错误: 商家 $MID 无上架菜品(menu 契约异常)" >&2
  exit 1
fi
echo "  dishId=$DISH_ID"

step "4. 加购+结算"
# 契约: POST /api/cart/items, body {dishId, quantity}(CartItemReq 无 merchantId)
curl -sf -X POST "$BASE/cart/items" -H "Authorization: Bearer $U_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"dishId\":$DISH_ID,\"quantity\":1}" | check_code
A=$(curl -sf -X POST "$BASE/user/addresses" -H "Authorization: Bearer $U_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"receiverName":"冒烟","receiverPhone":"13900001111","campusZone":"东区","detail":"1栋101","isDefault":1}')
echo "$A" | check_code
ADDR_ID=$(echo "$A" | json_get "['data']['id']")
# 契约: POST /api/orders/checkout -> Result<String>,data 直接为订单号(orderNo 字符串)
O=$(curl -sf -X POST "$BASE/orders/checkout" -H "Authorization: Bearer $U_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"merchantId\":$MID,\"addressId\":$ADDR_ID}")
echo "$O" | check_code
ORDER_NO=$(echo "$O" | json_get "['data']")
echo "  orderNo=$ORDER_NO"

step "5. 支付 + 模拟回调(两次验证幂等)"
curl -sf -X POST "$BASE/orders/$ORDER_NO/pay" -H "Authorization: Bearer $U_TOKEN" \
  -H 'Content-Type: application/json' -d '{"channel":"MOCK"}' | check_code
for i in 1 2; do
  C=$(curl -sf -X POST "$BASE/payment/mock/notify" -H 'Content-Type: application/json' \
    -d "{\"orderNo\":\"$ORDER_NO\",\"success\":true}" | check_code) && echo "  notify#$i code=0"
done
STATUS=$(curl -sf "$BASE/orders/$ORDER_NO" -H "Authorization: Bearer $U_TOKEN" | json_get "['data']['status']")
if [ "$STATUS" = "PAID" ]; then
  echo "  订单状态=PAID ✓"
else
  echo "[campus] 错误: 订单状态=$STATUS 期望 PAID" >&2
  exit 1
fi

step "6. 商家接单->出餐"
curl -sf -X POST "$BASE/merchant/orders/$ORDER_NO/accept" -H "Authorization: Bearer $M_TOKEN" | check_code
curl -sf -X POST "$BASE/merchant/orders/$ORDER_NO/ready" -H "Authorization: Bearer $M_TOKEN" | check_code
echo "  已接单并出餐"

step "7. 骑手接单->取餐->送达"
# 绝不选无关旧任务: 必须按当前订单号 orderNo 过滤。
# 派单策略: MockRiderDispatcher 预分配给 least-loaded 骑手(rider1 或 rider2),或留在开放池(rider_id=0)。
pick_owned_task() { # $1=token -> stdout "id status"(当前订单任务)或空
  local tok=$1
  curl -sf "$BASE/rider/tasks?page=1&size=100" -H "Authorization: Bearer $tok" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)['data']
for r in (d.get('records') or []):
    if r.get('orderNo') == '$ORDER_NO':
        print(r['id'], r.get('status', '')); break
"
}
pick_available_task() { # 开放池(rider_id=0)
  local tok=$1
  curl -sf "$BASE/rider/tasks/available" -H "Authorization: Bearer $tok" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)['data'] or []
for r in d:
    if r.get('orderNo') == '$ORDER_NO':
        print(r['id'], r.get('status', '')); break
"
}
TASK=""
ACTIVE_TOKEN=""
# 1) rider1 自有任务
if [ -z "$TASK" ]; then
  TASK=$(pick_owned_task "$R_TOKEN" 2>/dev/null || true)
  [ -n "$TASK" ] && ACTIVE_TOKEN="$R_TOKEN"
fi
# 2) rider2 自有任务(任务可能预分配给 least-loaded 的 rider2)
if [ -z "$TASK" ]; then
  R2=$(curl -sf -X POST "$BASE/auth/login" -H 'Content-Type: application/json' \
    -d '{"username":"rider2","password":"123456"}' 2>/dev/null || true)
  if echo "$R2" | json_get "['code']" 2>/dev/null | grep -q '^0$'; then
    R2_TOKEN=$(echo "$R2" | json_get "['data']['token']")
    TASK=$(pick_owned_task "$R2_TOKEN" 2>/dev/null || true)
    [ -n "$TASK" ] && ACTIVE_TOKEN="$R2_TOKEN"
  fi
fi
# 3) 开放池
if [ -z "$TASK" ]; then
  TASK=$(pick_available_task "$R_TOKEN" 2>/dev/null || true)
  [ -n "$TASK" ] && ACTIVE_TOKEN="$R_TOKEN"
fi
if [ -z "$TASK" ]; then
  echo "[campus] 错误: 未找到订单 $ORDER_NO 的配送任务(rider1/rider2 自有+开放池均无)" >&2
  exit 1
fi
TASK_ID=$(echo "$TASK" | awk '{print $1}')
TASK_STATUS=$(echo "$TASK" | awk '{print $2}')
echo "  taskId=$TASK_ID status=$TASK_STATUS (orderNo=$ORDER_NO)"
# 状态感知推进: 已 ACCEPTED 则跳过 accept;已 PICKED/DELIVERING 跳过 accept+pickup;已 DELIVERED 跳过全部
case "$TASK_STATUS" in
  WAIT_ACCEPT)
    curl -sf -X POST "$BASE/rider/tasks/$TASK_ID/accept" -H "Authorization: Bearer $ACTIVE_TOKEN" | check_code
    echo "  accept ok"
    curl -sf -X POST "$BASE/rider/tasks/$TASK_ID/pickup" -H "Authorization: Bearer $ACTIVE_TOKEN" | check_code
    echo "  pickup ok"
    ;;
  ACCEPTED)
    echo "  任务已是 ACCEPTED,跳过 accept"
    curl -sf -X POST "$BASE/rider/tasks/$TASK_ID/pickup" -H "Authorization: Bearer $ACTIVE_TOKEN" | check_code
    echo "  pickup ok"
    ;;
  PICKED|DELIVERING)
    echo "  任务已是 $TASK_STATUS,跳过 accept/pickup"
    ;;
  *)
    echo "[campus] 错误: 任务状态 $TASK_STATUS 不可推进" >&2
    exit 1
    ;;
esac
# deliver: PICKED/DELIVERING 均可推进(服务端两段转换+重读);已 DELIVERED 跳过
if [ "$TASK_STATUS" != "DELIVERED" ]; then
  curl -sf -X POST "$BASE/rider/tasks/$TASK_ID/deliver" -H "Authorization: Bearer $ACTIVE_TOKEN" | check_code
  echo "  deliver ok"
fi
STATUS=$(curl -sf "$BASE/orders/$ORDER_NO" -H "Authorization: Bearer $U_TOKEN" | json_get "['data']['status']")
if [ "$STATUS" = "COMPLETED" ]; then
  echo "  订单状态=COMPLETED ✓"
else
  echo "[campus] 错误: 订单状态=$STATUS 期望 COMPLETED" >&2
  exit 1
fi

step "8. 评价"
curl -sf -X POST "$BASE/orders/$ORDER_NO/review" -H "Authorization: Bearer $U_TOKEN" \
  -H 'Content-Type: application/json' -d '{"rating":5,"content":"冒烟测试好评"}' | check_code
echo "  review code=0"

step "9. 通知"
N=$(curl -sf "$BASE/user/notifications?page=1&size=10" -H "Authorization: Bearer $U_TOKEN" | json_get "['data']['total']")
echo "  通知条数=$N"

echo
echo "✅ 冒烟测试通过(链路完整)"
