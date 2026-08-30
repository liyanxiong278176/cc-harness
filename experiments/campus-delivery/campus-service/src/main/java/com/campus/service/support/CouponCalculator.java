package com.campus.service.support;

import com.campus.common.constant.Constants;
import com.campus.dao.entity.Coupon;
import com.campus.common.util.MoneyUtils;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 优惠券计算(纯逻辑,可单测;与 logic-harness/coupon.test.js 语义一致)。
 * 规则:
 *  - FULL_REDUCTION: 满 thresholdAmount 减 discountAmount,不设上限、不叠加。
 *  - DISCOUNT: 折后价 = 实付金额 * discountRate(0.900=9折),满 thresholdAmount 可用。
 */
public final class CouponCalculator {

    private CouponCalculator() {
    }

    /**
     * 计算优惠金额。
     *
     * @param coupon       券模板(需已校验可用)
     * @param goodsAmount  商品总额(不含运费)
     * @param now          当前时间(可注入便于测试)
     * @return 优惠金额(>=0)
     */
    public static BigDecimal discountAmount(Coupon coupon, BigDecimal goodsAmount, LocalDateTime now) {
        if (coupon == null || now == null || now.isBefore(coupon.getStartTime()) || now.isAfter(coupon.getEndTime())) {
            return BigDecimal.ZERO.setScale(MoneyUtils.SCALE);
        }
        if (MoneyUtils.compare(goodsAmount, coupon.getThresholdAmount()) < 0) {
            return BigDecimal.ZERO.setScale(MoneyUtils.SCALE);
        }
        if (Constants.CouponType.FULL_REDUCTION.equals(coupon.getType())) {
            return MoneyUtils.of(coupon.getDiscountAmount());
        }
        if (Constants.CouponType.DISCOUNT.equals(coupon.getType())) {
            // 优惠 = 商品额 * (1 - 折扣率)
            BigDecimal save = MoneyUtils.multiply(goodsAmount,
                    BigDecimal.ONE.subtract(MoneyUtils.of(coupon.getDiscountRate())));
            return MoneyUtils.of(save);
        }
        return BigDecimal.ZERO.setScale(MoneyUtils.SCALE);
    }

    /** 券是否在有效期内。 */
    public static boolean validNow(Coupon coupon, LocalDateTime now) {
        return coupon != null && now != null
                && !now.isBefore(coupon.getStartTime()) && !now.isAfter(coupon.getEndTime());
    }
}
