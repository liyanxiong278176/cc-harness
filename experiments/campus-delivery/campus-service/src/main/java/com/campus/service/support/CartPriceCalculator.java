package com.campus.service.support;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;

/**
 * 购物车/订单金额计算(纯逻辑,无外部依赖,便于单测与前端镜像)。
 * 金额一律 BigDecimal;逐行金额按 2 位小数半向下取整
 * (RoundingMode.HALF_DOWN: 恰好半数向下,如 0.105 → 0.10),
 * 合计与应付在已舍入的行金额上求和(天然为 2 位,末尾仅作防御性取整)。
 */
public final class CartPriceCalculator {

    private CartPriceCalculator() {
    }

    /** 购物车行(可同时用于订单明细快照计算)。 */
    public static class Line {
        private final BigDecimal price;
        private final int qty;

        public Line(BigDecimal price, int qty) {
            this.price = price == null ? BigDecimal.ZERO : price;
            this.qty = qty;
        }

        public BigDecimal getPrice() {
            return price;
        }

        public int getQty() {
            return qty;
        }
    }

    /** 计算结果(元,2 位小数)。 */
    public static class Result {
        private final BigDecimal goodsAmount;
        private final BigDecimal deliveryFee;
        private final BigDecimal discountAmount;
        private final BigDecimal payAmount;

        Result(BigDecimal goodsAmount, BigDecimal deliveryFee, BigDecimal discountAmount, BigDecimal payAmount) {
            this.goodsAmount = goodsAmount;
            this.deliveryFee = deliveryFee;
            this.discountAmount = discountAmount;
            this.payAmount = payAmount;
        }

        public BigDecimal getGoodsAmount() { return goodsAmount; }
        public BigDecimal getDeliveryFee() { return deliveryFee; }
        public BigDecimal getDiscountAmount() { return discountAmount; }
        public BigDecimal getPayAmount() { return payAmount; }
    }

    /** 商品合计 = Σ(单价 × 数量),逐行按 2 位小数半向下取整(0.105 → 0.10)。 */
    public static BigDecimal goodsAmount(List<Line> lines) {
        BigDecimal sum = BigDecimal.ZERO.setScale(2);
        for (Line l : lines) {
            sum = sum.add(l.price.multiply(BigDecimal.valueOf(l.qty)).setScale(2, RoundingMode.HALF_DOWN));
        }
        return sum.setScale(2, RoundingMode.HALF_DOWN);
    }

    /**
     * 应付金额 = 商品合计 + 配送费 - 优惠金额。
     * 优惠金额不允许超过商品合计(优惠截断),结果不小于 0。
     */
    public static Result calculate(List<Line> lines, BigDecimal deliveryFee, BigDecimal discountAmount) {
        BigDecimal goods = goodsAmount(lines);
        BigDecimal fee = deliveryFee == null ? BigDecimal.ZERO : deliveryFee;
        BigDecimal discount = discountAmount == null ? BigDecimal.ZERO : discountAmount;
        if (discount.compareTo(goods) > 0) {
            discount = goods;
        }
        BigDecimal pay = goods.add(fee).subtract(discount);
        if (pay.signum() < 0) {
            pay = BigDecimal.ZERO.setScale(2);
        }
        return new Result(goods, fee.setScale(2), discount.setScale(2), pay.setScale(2));
    }
}
