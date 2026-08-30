package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.common.model.PageQuery;
import com.campus.dao.entity.Dish;
import com.campus.dao.entity.DishCategory;
import com.campus.dao.entity.Merchant;
import com.campus.dao.entity.MerchantEmployee;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.entity.RefundRecord;
import com.campus.dao.mapper.DishCategoryMapper;
import com.campus.dao.mapper.DishMapper;
import com.campus.dao.mapper.MerchantEmployeeMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.RefundRecordMapper;
import com.campus.service.dto.BusinessStatusReq;
import com.campus.service.dto.CategoryReq;
import com.campus.service.dto.MerchantProfileReq;
import com.campus.service.dto.RefundReviewReq;
import com.campus.service.dto.ReviewReplyReq;
import com.campus.service.mq.OrderEventPublisher;
import com.campus.service.support.OrderStateMachine;
import com.campus.service.vo.CategoryVO;
import com.campus.service.vo.DashboardVO;
import com.campus.service.vo.MerchantVO;
import com.campus.service.vo.OrderVO;
import com.campus.service.vo.RefundVO;
import com.campus.service.vo.ReviewVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 商家服务: 浏览/资料/分类/营业状态/工作台统计。
 */
@Service
public class MerchantService {

    private final MerchantMapper merchantMapper;
    private final MerchantEmployeeMapper employeeMapper;
    private final DishCategoryMapper categoryMapper;
    private final DishMapper dishMapper;
    private final OrderInfoMapper orderInfoMapper;
    private final RefundRecordMapper refundRecordMapper;
    private final OrderService orderService;
    private final RefundService refundService;
    private final ReviewService reviewService;
    private final RiderService riderService;
    private final OrderEventPublisher orderEventPublisher;

    public MerchantService(MerchantMapper merchantMapper,
                           MerchantEmployeeMapper employeeMapper,
                           DishCategoryMapper categoryMapper,
                           DishMapper dishMapper,
                           OrderInfoMapper orderInfoMapper,
                           RefundRecordMapper refundRecordMapper,
                           OrderService orderService,
                           RefundService refundService,
                           ReviewService reviewService,
                           RiderService riderService,
                           OrderEventPublisher orderEventPublisher) {
        this.merchantMapper = merchantMapper;
        this.employeeMapper = employeeMapper;
        this.categoryMapper = categoryMapper;
        this.dishMapper = dishMapper;
        this.orderInfoMapper = orderInfoMapper;
        this.refundRecordMapper = refundRecordMapper;
        this.orderService = orderService;
        this.refundService = refundService;
        this.reviewService = reviewService;
        this.riderService = riderService;
        this.orderEventPublisher = orderEventPublisher;
    }

    // ---------- 浏览 ----------

    public PageResult<MerchantVO> page(String zone, PageQuery pq) {
        Page<Merchant> page = new Page<>(pq.getPage(), pq.getSize());
        LambdaQueryWrapper<Merchant> qw = new LambdaQueryWrapper<>();
        if (StringUtils.hasText(zone)) {
            qw.like(Merchant::getCampusZone, zone.trim());
        }
        qw.orderByDesc(Merchant::getIsOpen).orderByDesc(Merchant::getRating);
        Page<Merchant> result = merchantMapper.selectPage(page, qw);
        List<MerchantVO> vos = result.getRecords().stream()
                .map(MerchantService::toVO).collect(Collectors.toList());
        return PageResult.of(vos, result.getTotal(), pq.getSize(), pq.getPage());
    }

    public MerchantVO detail(Long merchantId) {
        Merchant m = merchantMapper.selectById(merchantId);
        if (m == null) {
            throw new BizException(ResultCode.MERCHANT_NOT_FOUND);
        }
        return toVO(m);
    }

    public Merchant requireMerchant(Long merchantId) {
        Merchant m = merchantMapper.selectById(merchantId);
        if (m == null) {
            throw new BizException(ResultCode.MERCHANT_NOT_FOUND);
        }
        return m;
    }

    /** 校验用户是否是该商家的员工。 */
    public MerchantEmployee requireEmployee(Long userId) {
        MerchantEmployee emp = employeeMapper.selectOne(new LambdaQueryWrapper<MerchantEmployee>()
                .eq(MerchantEmployee::getUserId, userId)
                .last("LIMIT 1"));
        if (emp == null) {
            throw new BizException(ResultCode.MERCHANT_NO_PERMISSION);
        }
        return emp;
    }

    // ---------- 商家管理 ----------

    public MerchantVO myProfile(Long userId) {
        MerchantEmployee emp = requireEmployee(userId);
        return detail(emp.getMerchantId());
    }

    @Transactional
    public MerchantVO updateProfile(Long userId, MerchantProfileReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        Merchant upd = new Merchant();
        upd.setId(emp.getMerchantId());
        if (StringUtils.hasText(req.getName())) {
            upd.setName(req.getName().trim());
        }
        if (StringUtils.hasText(req.getLogo())) {
            upd.setLogo(req.getLogo().trim());
        }
        if (StringUtils.hasText(req.getDescription())) {
            upd.setDescription(req.getDescription().trim());
        }
        if (StringUtils.hasText(req.getCategory())) {
            upd.setCategory(req.getCategory().trim());
        }
        if (StringUtils.hasText(req.getCampusZone())) {
            upd.setCampusZone(req.getCampusZone().trim());
        }
        if (req.getDeliveryFee() != null) {
            upd.setDeliveryFee(req.getDeliveryFee());
        }
        if (req.getMinOrderAmount() != null) {
            upd.setMinOrderAmount(req.getMinOrderAmount());
        }
        if (StringUtils.hasText(req.getOpenTime())) {
            upd.setOpenTime(req.getOpenTime().trim());
        }
        if (StringUtils.hasText(req.getCloseTime())) {
            upd.setCloseTime(req.getCloseTime().trim());
        }
        merchantMapper.updateById(upd);
        return detail(emp.getMerchantId());
    }

    @Transactional
    public void setBusinessStatus(Long userId, BusinessStatusReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        Merchant upd = new Merchant();
        upd.setId(emp.getMerchantId());
        upd.setIsOpen(req.getIsOpen() == null || req.getIsOpen() != 1 ? 0 : 1);
        merchantMapper.updateById(upd);
    }

    // ---------- 分类 ----------

    public List<CategoryVO> listCategories(Long userId) {
        MerchantEmployee emp = requireEmployee(userId);
        return listCategoriesByMerchant(emp.getMerchantId());
    }

    public List<CategoryVO> listCategoriesByMerchant(Long merchantId) {
        List<DishCategory> list = categoryMapper.selectList(new LambdaQueryWrapper<DishCategory>()
                .eq(DishCategory::getMerchantId, merchantId)
                .orderByAsc(DishCategory::getSortOrder)
                .orderByAsc(DishCategory::getId));
        return list.stream().map(c -> {
            CategoryVO vo = new CategoryVO();
            vo.setId(c.getId());
            vo.setName(c.getName());
            vo.setSortOrder(c.getSortOrder());
            vo.setStatus(c.getStatus());
            return vo;
        }).collect(Collectors.toList());
    }

    @Transactional
    public CategoryVO addCategory(Long userId, CategoryReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        DishCategory c = new DishCategory();
        c.setMerchantId(emp.getMerchantId());
        c.setName(req.getName().trim());
        c.setSortOrder(req.getSortOrder() == null ? 0 : req.getSortOrder());
        c.setStatus(1);
        categoryMapper.insert(c);
        return toCategoryVO(c);
    }

    @Transactional
    public CategoryVO updateCategory(Long userId, Long id, CategoryReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        DishCategory exist = categoryMapper.selectById(id);
        if (exist == null || !exist.getMerchantId().equals(emp.getMerchantId())) {
            throw new BizException(ResultCode.CATEGORY_NOT_FOUND);
        }
        DishCategory upd = new DishCategory();
        upd.setId(id);
        upd.setName(req.getName().trim());
        upd.setSortOrder(req.getSortOrder() == null ? exist.getSortOrder() : req.getSortOrder());
        categoryMapper.updateById(upd);
        return toCategoryVO(categoryMapper.selectById(id));
    }

    @Transactional
    public void deleteCategory(Long userId, Long id) {
        MerchantEmployee emp = requireEmployee(userId);
        DishCategory exist = categoryMapper.selectById(id);
        if (exist == null || !exist.getMerchantId().equals(emp.getMerchantId())) {
            throw new BizException(ResultCode.CATEGORY_NOT_FOUND);
        }
        Long cnt = dishMapper.selectCount(new LambdaQueryWrapper<Dish>()
                .eq(Dish::getCategoryId, id));
        if (cnt != null && cnt > 0) {
            throw new BizException(ResultCode.CATEGORY_HAS_DISHES);
        }
        categoryMapper.deleteById(id);
    }

    // ---------- 工作台 ----------

    public DashboardVO dashboard(Long userId) {
        MerchantEmployee emp = requireEmployee(userId);
        Long merchantId = emp.getMerchantId();
        DashboardVO vo = new DashboardVO();
        LocalDateTime todayStart = LocalDate.now().atStartOfDay();
        LocalDateTime todayEnd = LocalDate.now().atTime(LocalTime.MAX);
        vo.setTodayOrderCount(orderInfoMapper.selectCount(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getMerchantId, merchantId)
                .ge(OrderInfo::getCreatedAt, todayStart)
                .le(OrderInfo::getCreatedAt, todayEnd)));
        vo.setTodayAmount(aggregateAmount(merchantId, todayStart, todayEnd));
        vo.setMonthAmount(aggregateAmount(merchantId,
                LocalDate.now().withDayOfMonth(1).atStartOfDay(), todayEnd));
        vo.setPendingAcceptCount(orderInfoMapper.selectCount(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getMerchantId, merchantId)
                .eq(OrderInfo::getStatus, "PAID")));
        vo.setPendingRefundCount(refundRecordMapper.selectCount(new LambdaQueryWrapper<RefundRecord>()
                .eq(RefundRecord::getStatus, com.campus.common.constant.Constants.RefundStatus.PENDING)));
        vo.setTotalDishCount(dishMapper.selectCount(new LambdaQueryWrapper<Dish>()
                .eq(Dish::getMerchantId, merchantId)));
        return vo;
    }

    private BigDecimal aggregateAmount(Long merchantId, LocalDateTime from, LocalDateTime to) {
        BigDecimal sum = orderInfoMapper.sumPayAmount(merchantId, from, to);
        return sum == null ? BigDecimal.ZERO : sum;
    }

    // ---------- 订单/评价/退款(商家后台) ----------

    /** 商家订单分页。 */
    public PageResult<OrderVO> pageOrders(Long userId, String status, PageQuery pq) {
        requireEmployee(userId);
        Merchant m = requireMerchant(requireEmployee(userId).getMerchantId());
        return orderService.pageOrdersForMerchant(m.getId(), status, pq);
    }

    /** 接单: PAID -> PREPARING。 */
    @Transactional
    public void acceptOrder(Long userId, String orderNo) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        OrderInfo order = requireOrder(orderNo);
        if (!order.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!OrderStateMachine.canTransit(order.getStatus(), Constants.OrderStatus.PREPARING)) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        int rows = orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, order.getId())
                .eq(OrderInfo::getStatus, Constants.OrderStatus.PAID)
                .set(OrderInfo::getStatus, Constants.OrderStatus.PREPARING)
                .set(OrderInfo::getAcceptTime, LocalDateTime.now()));
        if (rows == 0) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        orderEventPublisher.publish(orderNo, order.getUserId(), merchantId,
                Constants.Mq.RK_ORDER_STATUS, "商家已接单，开始备餐",
                Constants.NotificationType.ORDER_STATUS, "ORDER_ACCEPTED");
    }

    /** 出餐: PREPARING -> DELIVERING,并触发派单。 */
    @Transactional
    public void readyOrder(Long userId, String orderNo) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        OrderInfo order = requireOrder(orderNo);
        if (!order.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!OrderStateMachine.canTransit(order.getStatus(), Constants.OrderStatus.DELIVERING)) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        int rows = orderInfoMapper.update(null, new LambdaUpdateWrapper<OrderInfo>()
                .eq(OrderInfo::getId, order.getId())
                .eq(OrderInfo::getStatus, Constants.OrderStatus.PREPARING)
                .set(OrderInfo::getStatus, Constants.OrderStatus.DELIVERING));
        if (rows == 0) {
            throw new BizException(ResultCode.ORDER_STATUS_INVALID);
        }
        Merchant merchant = merchantMapper.selectById(merchantId);
        String pickup = (merchant != null ? merchant.getName() : "店铺#" + merchantId) + "(" + (merchant != null ? merchant.getCampusZone() : "") + ")";
        riderService.dispatch(order, pickup, order.getAddressSnapshot());
        orderEventPublisher.publish(orderNo, order.getUserId(), merchantId,
                Constants.Mq.RK_ORDER_STATUS, "商家已出餐，配送中",
                Constants.NotificationType.ORDER_STATUS, "ORDER_DELIVERING");
    }

    /** 商家评价列表。 */
    public PageResult<ReviewVO> listReviews(Long userId, PageQuery pq) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        return reviewService.pageByMerchant(merchantId, pq);
    }

    /** 商家回复评价。 */
    public void replyReview(Long userId, Long id, ReviewReplyReq req) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        reviewService.reply(merchantId, id, req);
    }

    /** 商家退款列表。 */
    public PageResult<RefundVO> listRefunds(Long userId, PageQuery pq) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        return refundService.pageByMerchant(merchantId, pq);
    }

    /** 同意退款。 */
    public void approveRefund(Long userId, Long id) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        refundService.approve(merchantId, id);
    }

    /** 拒绝退款。 */
    public void rejectRefund(Long userId, Long id, RefundReviewReq req) {
        Long merchantId = requireEmployee(userId).getMerchantId();
        refundService.reject(merchantId, id, req.getReason());
    }

    private OrderInfo requireOrder(String orderNo) {
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        if (order == null) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        return order;
    }

    public static MerchantVO toVO(Merchant m) {
        MerchantVO vo = new MerchantVO();
        vo.setId(m.getId());
        vo.setName(m.getName());
        vo.setLogo(m.getLogo());
        vo.setDescription(m.getDescription());
        vo.setCategory(m.getCategory());
        vo.setCampusZone(m.getCampusZone());
        vo.setDeliveryFee(m.getDeliveryFee());
        vo.setMinOrderAmount(m.getMinOrderAmount());
        vo.setOpenTime(m.getOpenTime());
        vo.setCloseTime(m.getCloseTime());
        vo.setIsOpen(m.getIsOpen());
        vo.setRating(m.getRating());
        vo.setRatingCount(m.getRatingCount());
        return vo;
    }

    private static CategoryVO toCategoryVO(DishCategory c) {
        CategoryVO vo = new CategoryVO();
        vo.setId(c.getId());
        vo.setName(c.getName());
        vo.setSortOrder(c.getSortOrder());
        vo.setStatus(c.getStatus());
        return vo;
    }
}
