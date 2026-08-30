package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.api.PageResult;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.common.model.PageQuery;
import com.campus.common.util.MoneyUtils;
import com.campus.dao.entity.Coupon;
import com.campus.dao.entity.UserCoupon;
import com.campus.dao.mapper.CouponMapper;
import com.campus.dao.mapper.UserCouponMapper;
import com.campus.service.support.CouponCalculator;
import com.campus.service.vo.CouponVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 优惠券服务: 领券 / 我的券 / 结算校验与核销。
 */
@Service
public class CouponService {

    private final CouponMapper couponMapper;
    private final UserCouponMapper userCouponMapper;

    public CouponService(CouponMapper couponMapper, UserCouponMapper userCouponMapper) {
        this.couponMapper = couponMapper;
        this.userCouponMapper = userCouponMapper;
    }

    /** 可领取的券模板(进行中且未领完)。 */
    public PageResult<CouponVO> availableCoupons(PageQuery pq) {
        var page = new com.baomidou.mybatisplus.extension.plugins.pagination.Page<Coupon>(pq.getPage(), pq.getSize());
        LambdaQueryWrapper<Coupon> qw = new LambdaQueryWrapper<Coupon>()
                .eq(Coupon::getStatus, 1)
                .le(Coupon::getStartTime, LocalDateTime.now())
                .ge(Coupon::getEndTime, LocalDateTime.now())
                .orderByDesc(Coupon::getId);
        var result = couponMapper.selectPage(page, qw);
        return PageResult.of(result.getRecords().stream()
                .map(CouponService::toVOTemplate).collect(Collectors.toList()),
                result.getTotal(), pq.getSize(), pq.getPage());
    }

    /** 我的券(UNUSED/USED/EXPIRED)。 */
    public List<CouponVO> myCoupons(Long userId, String status) {
        List<UserCoupon> mine = userCouponMapper.selectList(new LambdaQueryWrapper<UserCoupon>()
                .eq(UserCoupon::getUserId, userId)
                .orderByDesc(UserCoupon::getId));
        // 延迟过期: 已到 endTime 仍未用的置为 EXPIRED(不写库,展示层处理)
        return mine.stream().map(uc -> {
            Coupon c = couponMapper.selectById(uc.getCouponId());
            CouponVO vo = toVO(uc, c);
            if (Constants.UserCouponStatus.UNUSED.equals(vo.getStatus())
                    && vo.getEndTime() != null
                    && vo.getEndTime().isBefore(LocalDateTime.now())) {
                vo.setStatus(Constants.UserCouponStatus.EXPIRED);
            }
            return vo;
        }).filter(vo -> status == null || status.isBlank() || status.equals(vo.getStatus()))
                .collect(Collectors.toList());
    }

    @Transactional
    public void receive(Long userId, Long couponId) {
        Coupon coupon = couponMapper.selectById(couponId);
        if (coupon == null || coupon.getStatus() == null || coupon.getStatus() != 1) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        LocalDateTime now = LocalDateTime.now();
        if (now.isBefore(coupon.getStartTime()) || now.isAfter(coupon.getEndTime())) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        Long owned = userCouponMapper.selectCount(new LambdaQueryWrapper<UserCoupon>()
                .eq(UserCoupon::getUserId, userId)
                .eq(UserCoupon::getCouponId, couponId));
        if (owned != null && owned > 0) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        // 领券量条件更新(防超发)
        int rows = couponMapper.incrementIssued(couponId, 1, coupon.getTotalCount());
        if (rows == 0) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        UserCoupon uc = new UserCoupon();
        uc.setUserId(userId);
        uc.setCouponId(couponId);
        uc.setStatus(Constants.UserCouponStatus.UNUSED);
        uc.setExpireAt(coupon.getEndTime());
        userCouponMapper.insert(uc);
    }

    /**
     * 结算时校验用户券(只读,不改状态),返回券模板。
     * 核销由 {@link #markUsed} 在订单落库后调用。
     */
    public Coupon validateCoupon(Long userId, Long userCouponId, java.math.BigDecimal goodsAmount) {
        UserCoupon uc = userCouponMapper.selectById(userCouponId);
        if (uc == null || !uc.getUserId().equals(userId)
                || !Constants.UserCouponStatus.UNUSED.equals(uc.getStatus())) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        Coupon coupon = couponMapper.selectById(uc.getCouponId());
        if (coupon == null || !CouponCalculator.validNow(coupon, LocalDateTime.now())) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        if (MoneyUtils.compare(goodsAmount, coupon.getThresholdAmount()) < 0) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        return coupon;
    }

    /** 核销用户券(条件更新 UNUSED->USED);返回 0 表示并发冲突。 */
    public int markUsed(Long userId, Long userCouponId, Long orderId) {
        UserCoupon uc = userCouponMapper.selectById(userCouponId);
        if (uc == null || !uc.getUserId().equals(userId)) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        int rows = userCouponMapper.markUsed(uc.getId(), orderId, uc.getVersion());
        if (rows == 0) {
            throw new BizException(ResultCode.COUPON_INVALID);
        }
        return rows;
    }

    /** 退款/取消时退还券(USED -> UNUSED)。 */
    @Transactional
    public void releaseCoupon(Long userId, Long userCouponId, Long orderId) {
        if (userCouponId == null) {
            return;
        }
        userCouponMapper.release(orderId, userId);
    }

    private static CouponVO toVOTemplate(Coupon c) {
        CouponVO vo = new CouponVO();
        vo.setId(c.getId());
        vo.setName(c.getName());
        vo.setType(c.getType());
        vo.setThresholdAmount(c.getThresholdAmount());
        vo.setDiscountAmount(c.getDiscountAmount());
        vo.setDiscountRate(c.getDiscountRate());
        vo.setStartTime(c.getStartTime());
        vo.setEndTime(c.getEndTime());
        return vo;
    }

    private static CouponVO toVO(UserCoupon uc, Coupon c) {
        CouponVO vo = toVOTemplate(c);
        vo.setId(uc.getId());
        vo.setStatus(uc.getStatus());
        vo.setExpireAt(uc.getExpireAt());
        return vo;
    }
}
