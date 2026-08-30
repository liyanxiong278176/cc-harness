package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.common.log.OperLog;
import com.campus.common.model.PageQuery;
import com.campus.common.util.IdUtils;
import com.campus.common.util.JsonUtils;
import com.campus.common.util.MoneyUtils;
import com.campus.dao.entity.Cart;
import com.campus.dao.entity.Dish;
import com.campus.dao.entity.Merchant;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.entity.OrderItem;
import com.campus.dao.entity.PaymentRecord;
import com.campus.dao.entity.Review;
import com.campus.dao.entity.StockChangeLog;
import com.campus.dao.entity.UserAddress;
import com.campus.dao.entity.UserCoupon;
import com.campus.dao.mapper.CartMapper;
import com.campus.dao.mapper.DishMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.OrderItemMapper;
import com.campus.dao.mapper.PaymentRecordMapper;
import com.campus.dao.mapper.ReviewMapper;
import com.campus.dao.mapper.StockChangeLogMapper;
import com.campus.dao.mapper.UserAddressMapper;
import com.campus.dao.mapper.UserCouponMapper;
import com.campus.service.dto.CheckoutReq;
import com.campus.service.mq.OrderEventPublisher;
import com.campus.service.support.OrderStateMachine;
import com.campus.service.vo.OrderDetailVO;
import com.campus.service.vo.OrderVO;
import com.campus.service.vo.TrackVO;
import com.campus.common.util.AesUtils;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * 订单服务: 结算下单(事务内扣库存/核销券/写 outbox)、取消、列表、详情、跟踪。
 * 防超卖: 见 docs/stock-and-concurrency.md(条件更新 + 乐观锁)。
 */
@Service
public class OrderService {

    private final OrderInfoMapper orderInfoMapper;
    private final OrderItemMapper orderItemMapper;
    private final CartMapper cartMapper;
    private final DishMapper dishMapper;
    private final MerchantMapper merchantMapper;
    private final UserAddressMapper addressMapper;
    private final UserCouponMapper userCouponMapper;
    private final StockChangeLogMapper stockChangeLogMapper;
    private final PaymentRecordMapper paymentRecordMapper;
    private final ReviewMapper reviewMapper;
    private final CouponService couponService;
    private final OrderEventPublisher orderEventPublisher;
    private final RefundService refundService;
    private final ReviewService reviewService;

    public OrderService(OrderInfoMapper orderInfoMapper,
                        OrderItemMapper orderItemMapper,
                        CartMapper cartMapper,
                        DishMapper dishMapper,
                        MerchantMapper merchantMapper,
                        UserAddressMapper addressMapper,
                        UserCouponMapper userCouponMapper,
                        StockChangeLogMapper stockChangeLogMapper,
                        PaymentRecordMapper paymentRecordMapper,
                        ReviewMapper reviewMapper,
                        CouponService couponService,
                        OrderEventPublisher orderEventPublisher,
                        RefundService refundService,
                        ReviewService reviewService) {
        this.orderInfoMapper = orderInfoMapper;
        this.orderItemMapper = orderItemMapper;
        this.cartMapper = cartMapper;
        this.dishMapper = dishMapper;
        this.merchantMapper = merchantMapper;
        this.addressMapper = addressMapper;
        this.userCouponMapper = userCouponMapper;
        this.stockChangeLogMapper = stockChangeLogMapper;
        this.paymentRecordMapper = paymentRecordMapper;
        this.reviewMapper = reviewMapper;
        this.couponService = couponService;
        this.orderEventPublisher = orderEventPublisher;
        this.refundService = refundService;
        this.reviewService = reviewService;
    }

    @OperLog(value = "结算下单", module = "order")
    @Transactional
    public String checkout(Long userId, CheckoutReq req) {
        Merchant merchant = merchantMapper.selectById(req.getMerchantId());
        if (merchant == null) {
            throw new BizException(ResultCode.MERCHANT_NOT_FOUND);
        }
        if (merchant.getIsOpen() == null || merchant.getIsOpen() != 1) {
            throw new BizException(ResultCode.MERCHANT_CLOSED);
        }
        UserAddress address = addressMapper.selectById(req.getAddressId());
        if (address == null || !address.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ADDRESS_NOT_OWNED);
        }
        List<Cart> rows = cartMapper.selectList(new LambdaQueryWrapper<Cart>()
                .eq(Cart::getUserId, userId)
                .eq(Cart::getMerchantId, req.getMerchantId())
                .eq(Cart::getChecked, 1));
        if (rows.isEmpty()) {
            throw new BizException(ResultCode.CART_EMPTY);
        }
        List<Cart> invalidStatus = rows.stream()
                .filter(r -> dishMapper.selectById(r.getDishId()) == null)
                .collect(Collectors.toList());
        if (!invalidStatus.isEmpty()) {
            throw new BizException(ResultCode.DISH_NOT_FOUND);
        }

        // ---- 金额计算 ----
        BigDecimal goodsAmount = BigDecimal.ZERO.setScale(MoneyUtils.SCALE);
        Map<Long, Dish> dishes = new LinkedHashMap<>();
        for (Cart r : rows) {
            Dish d = dishMapper.selectById(r.getDishId());
            dishes.put(d.getId(), d);
            goodsAmount = MoneyUtils.add(goodsAmount, MoneyUtils.multiply(d.getPrice(), BigDecimal.valueOf(r.getQuantity())));
        }
        // 起送校验
        if (MoneyUtils.compare(goodsAmount, merchant.getMinOrderAmount()) < 0) {
            throw new BizException(ResultCode.BELOW_MIN_AMOUNT);
        }
        // 券校验(只读,核销在订单落库后进行)
        BigDecimal discount = BigDecimal.ZERO.setScale(MoneyUtils.SCALE);
        Long userCouponId = req.getCouponId();
        if (userCouponId != null) {
            com.campus.dao.entity.Coupon coupon = couponService.validateCoupon(userId, userCouponId, goodsAmount);
            discount = MoneyUtils.of(com.campus.service.support.CouponCalculator.discountAmount(
                    coupon, goodsAmount, LocalDateTime.now()));
        }
        BigDecimal deliveryFee = merchant.getDeliveryFee() == null ? BigDecimal.ZERO : MoneyUtils.of(merchant.getDeliveryFee());
        BigDecimal payAmount = MoneyUtils.subtract(MoneyUtils.add(goodsAmount, deliveryFee), discount);

        // ---- 创建订单 ----
        String orderNo = IdUtils.orderNo();
        OrderInfo order = new OrderInfo();
        order.setOrderNo(orderNo);
        order.setUserId(userId);
        order.setMerchantId(merchant.getId());
        order.setAddressId(address.getId());
        order.setAddressSnapshot(JsonUtils.toJson(addressSnap(address)));
        order.setCouponId(userCouponId == null ? 0L : userCouponId);
        order.setTotalAmount(goodsAmount);
        order.setDeliveryFee(deliveryFee);
        order.setDiscountAmount(discount);
        order.setPayAmount(payAmount);
        order.setStatus(Constants.OrderStatus.CREATED);
        order.setRemark(req.getRemark());
        orderInfoMapper.insert(order);

        // ---- 订单明细 + 扣库存(条件更新防超卖) ----
        for (Cart r : rows) {
            Dish d = dishes.get(r.getDishId());
            int affected = dishMapper.deductStock(d.getId(), r.getQuantity(), d.getVersion());
            if (affected == 0) {
                throw new BizException(ResultCode.STOCK_NOT_ENOUGH);
            }
            OrderItem item = new OrderItem();
            item.setOrderId(order.getId());
            item.setDishId(d.getId());
            item.setDishNameSnapshot(d.getName());
            item.setDishPriceSnapshot(d.getPrice());
            item.setQuantity(r.getQuantity());
            item.setSubtotal(MoneyUtils.multiply(d.getPrice(), BigDecimal.valueOf(r.getQuantity())));
            orderItemMapper.insert(item);
            stockChangeLogMapper.insert(stockLog(d.getId(), order.getId(), Constants.StockChangeType.DEDUCT,
                    r.getQuantity(), d.getStock(), d.getStock() - r.getQuantity()));
        }

        // ---- 核销优惠券(条件更新,失败则回滚整单) ----
        if (userCouponId != null) {
            couponService.markUsed(userId, userCouponId, order.getId());
        }

        // ---- 清空已购购物车 ----
        for (Cart r : rows) {
            cartMapper.deleteById(r.getId());
        }

        // ---- 事务内写 outbox,提交后由 relay 可靠投递 ----
        orderEventPublisher.publish(orderNo, userId, merchant.getId(),
                Constants.Mq.RK_ORDER_CREATED,
                "订单已创建", Constants.NotificationType.ORDER_STATUS, "ORDER_CREATED");
        return orderNo;
    }

    @OperLog(value = "取消订单", module = "order")
    @Transactional
    public void cancel(Long userId, String orderNo, String reason) {
        OrderInfo order = requireOrder(orderNo);
        if (!order.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!OrderStateMachine.canTransit(order.getStatus(), Constants.OrderStatus.CANCELLED)) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        OrderInfo upd = new OrderInfo();
        upd.setId(order.getId());
        upd.setStatus(Constants.OrderStatus.CANCELLED);
        upd.setCancelReason(reason);
        upd.setCancelTime(LocalDateTime.now());
        orderInfoMapper.updateById(upd);
        // 回滚库存 + 退券
        restoreStock(order.getId());
        couponService.releaseCoupon(userId, order.getCouponId(), order.getId());
        orderEventPublisher.publish(orderNo, order.getUserId(), order.getMerchantId(),
                Constants.Mq.RK_ORDER_STATUS, "订单已取消",
                Constants.NotificationType.ORDER_STATUS, "ORDER_CANCELLED");
    }

    public PageResult<OrderVO> pageOrders(Long userId, String status, PageQuery pq) {
        Page<OrderInfo> page = new Page<>(pq.getPage(), pq.getSize());
        LambdaQueryWrapper<OrderInfo> qw = new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getUserId, userId);
        if (StringUtils.hasText(status)) {
            qw.eq(OrderInfo::getStatus, status);
        }
        qw.orderByDesc(OrderInfo::getId);
        Page<OrderInfo> result = orderInfoMapper.selectPage(page, qw);
        List<OrderVO> vos = result.getRecords().stream()
                .map(this::toOrderVO).collect(Collectors.toList());
        return PageResult.of(vos, result.getTotal(), pq.getSize(), pq.getPage());
    }

    public OrderDetailVO detail(Long userId, String orderNo) {
        OrderInfo order = requireOrder(orderNo);
        if (!order.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        return buildDetail(order);
    }

    public OrderDetailVO detailForMerchant(Long merchantId, String orderNo) {
        OrderInfo order = requireOrder(orderNo);
        if (!order.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        return buildDetail(order);
    }

    public TrackVO track(Long userId, String orderNo) {
        OrderInfo order = requireOrder(orderNo);
        if (!order.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        TrackVO vo = new TrackVO();
        vo.setOrderNo(order.getOrderNo());
        vo.setOrderStatus(order.getStatus());
        vo.setPayAmount(order.getPayAmount());
        vo.setCreatedAt(order.getCreatedAt());
        vo.setPayTime(order.getPayTime());
        vo.setCancelTime(order.getCancelTime());
        vo.setDeliveredTime(order.getDeliveredTime());
        PaymentRecord pay = paymentRecordMapper.selectOne(new LambdaQueryWrapper<PaymentRecord>()
                .eq(PaymentRecord::getOrderNo, orderNo).last("LIMIT 1"));
        if (pay != null) {
            vo.setPayStatus(pay.getStatus());
        }
        return vo;
    }

    // ---------- 内部 ----------

    /** 申请退款(仅 PAID/PREPARING/DELIVERING),委托 RefundService。 */
    public void applyRefund(Long userId, String orderNo, com.campus.service.dto.RefundReq req) {
        refundService.apply(userId, orderNo, req.getReason());
    }

    /** 商家视角订单分页(归属校验在 MerchantService),委托本服务复用订单映射。 */
    public PageResult<OrderVO> pageOrdersForMerchant(Long merchantId, String status, PageQuery pq) {
        Page<OrderInfo> page = new Page<>(pq.getPage(), pq.getSize());
        LambdaQueryWrapper<OrderInfo> qw = new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getMerchantId, merchantId);
        if (StringUtils.hasText(status)) {
            qw.eq(OrderInfo::getStatus, status);
        }
        qw.orderByDesc(OrderInfo::getId);
        Page<OrderInfo> result = orderInfoMapper.selectPage(page, qw);
        List<OrderVO> vos = result.getRecords().stream().map(this::toOrderVO).collect(Collectors.toList());
        return PageResult.of(vos, result.getTotal(), pq.getSize(), pq.getPage());
    }

    /** 完成订单评价,委托 ReviewService。 */
    public void createReview(Long userId, String orderNo, com.campus.service.dto.ReviewCreateReq req) {
        reviewService.create(userId, orderNo, req);
    }

    public OrderInfo requireOrder(String orderNo) {
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        if (order == null) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        return order;
    }

    private OrderDetailVO buildDetail(OrderInfo order) {
        OrderDetailVO vo = new OrderDetailVO();
        vo.setId(order.getId());
        vo.setOrderNo(order.getOrderNo());
        vo.setMerchantId(order.getMerchantId());
        Merchant m = merchantMapper.selectById(order.getMerchantId());
        vo.setMerchantName(m == null ? "店铺#" + order.getMerchantId() : m.getName());
        vo.setStatus(order.getStatus());
        vo.setTotalAmount(order.getTotalAmount());
        vo.setDiscountAmount(order.getDiscountAmount());
        vo.setDeliveryFee(order.getDeliveryFee());
        vo.setPayAmount(order.getPayAmount());
        vo.setRemark(order.getRemark());
        vo.setCancelReason(order.getCancelReason());
        vo.setCreatedAt(order.getCreatedAt());
        vo.setPayTime(order.getPayTime());
        vo.setRiderId(order.getRiderId());
        Map<String, Object> snap = JsonUtils.parseMap(order.getAddressSnapshot());
        vo.setReceiverName(snap.get("receiverName") == null ? null : String.valueOf(snap.get("receiverName")));
        vo.setReceiverPhone(snap.get("receiverPhone") == null ? null : AesUtils.decrypt(String.valueOf(snap.get("receiverPhone"))));
        vo.setReceiverAddress(snap.get("detail") == null ? null : String.valueOf(snap.get("detail")));
        List<OrderItem> items = orderItemMapper.selectList(new LambdaQueryWrapper<OrderItem>()
                .eq(OrderItem::getOrderId, order.getId()));
        vo.setItems(items.stream().map(i -> {
            OrderDetailVO.Item it = new OrderDetailVO.Item();
            it.setDishName(i.getDishNameSnapshot());
            it.setPrice(i.getDishPriceSnapshot());
            it.setQuantity(i.getQuantity());
            it.setSubtotal(i.getSubtotal());
            return it;
        }).collect(Collectors.toList()));
        Long reviewed = reviewMapper.selectCount(new LambdaQueryWrapper<Review>()
                .eq(Review::getOrderId, order.getId()));
        vo.setReviewed(reviewed != null && reviewed > 0);
        return vo;
    }

    private OrderVO toOrderVO(OrderInfo o) {
        OrderVO vo = new OrderVO();
        vo.setId(o.getId());
        vo.setOrderNo(o.getOrderNo());
        vo.setMerchantId(o.getMerchantId());
        Merchant m = merchantMapper.selectById(o.getMerchantId());
        vo.setMerchantName(m == null ? "店铺#" + o.getMerchantId() : m.getName());
        vo.setStatus(o.getStatus());
        vo.setTotalAmount(o.getTotalAmount());
        vo.setDiscountAmount(o.getDiscountAmount());
        vo.setDeliveryFee(o.getDeliveryFee());
        vo.setPayAmount(o.getPayAmount());
        vo.setRemark(o.getRemark());
        vo.setCreatedAt(o.getCreatedAt());
        Long cnt = orderItemMapper.selectCount(new LambdaQueryWrapper<OrderItem>()
                .eq(OrderItem::getOrderId, o.getId()));
        vo.setItemCount(cnt == null ? 0 : cnt.intValue());
        return vo;
    }

    private void restoreStock(Long orderId) {
        List<OrderItem> items = orderItemMapper.selectList(new LambdaQueryWrapper<OrderItem>()
                .eq(OrderItem::getOrderId, orderId));
        for (OrderItem it : items) {
            Dish d = dishMapper.selectById(it.getDishId());
            dishMapper.rollbackStock(it.getDishId(), it.getQuantity());
            if (d != null) {
                stockChangeLogMapper.insert(stockLog(it.getDishId(), orderId, Constants.StockChangeType.ROLLBACK,
                        it.getQuantity(), d.getStock(), d.getStock() + it.getQuantity()));
            }
        }
    }

    private static StockChangeLog stockLog(Long dishId, Long orderId, String type,
                                           int qty, int before, int after) {
        StockChangeLog log = new StockChangeLog();
        log.setDishId(dishId);
        log.setOrderId(orderId);
        log.setChangeType(type);
        log.setChangeQty(qty);
        log.setBeforeStock(before);
        log.setAfterStock(after);
        return log;
    }

    private static Map<String, Object> addressSnap(UserAddress a) {
        Map<String, Object> snap = new LinkedHashMap<>();
        snap.put("receiverName", a.getReceiverName());
        snap.put("receiverPhone", a.getReceiverPhone());
        snap.put("campusZone", a.getCampusZone());
        snap.put("detail", a.getDetail());
        return snap;
    }
}
