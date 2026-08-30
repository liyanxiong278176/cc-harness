package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.model.PageQuery;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.dao.entity.Merchant;
import com.campus.dao.entity.OrderInfo;
import com.campus.dao.entity.Review;
import com.campus.dao.entity.SysUser;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.OrderInfoMapper;
import com.campus.dao.mapper.ReviewMapper;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.service.dto.ReviewCreateReq;
import com.campus.service.dto.ReviewReplyReq;
import com.campus.service.vo.ReviewVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 评价服务(评价域): 完成订单评价 + 商家评分聚合 + 商家回复。
 * 幂等: review.order_id 唯一键(uk_order)兜底,先查后插。
 * 商家评分聚合使用单条 SQL 表达式原子更新(避免读-改-写竞态)。
 */
@Service
public class ReviewService {

    private final ReviewMapper reviewMapper;
    private final OrderInfoMapper orderInfoMapper;
    private final SysUserMapper sysUserMapper;
    private final MerchantMapper merchantMapper;

    public ReviewService(ReviewMapper reviewMapper,
                         OrderInfoMapper orderInfoMapper,
                         SysUserMapper sysUserMapper,
                         MerchantMapper merchantMapper) {
        this.reviewMapper = reviewMapper;
        this.orderInfoMapper = orderInfoMapper;
        this.sysUserMapper = sysUserMapper;
        this.merchantMapper = merchantMapper;
    }

    /** 用户评价已完成订单。 */
    @Transactional
    public void create(Long userId, String orderNo, ReviewCreateReq req) {
        OrderInfo order = orderInfoMapper.selectOne(new LambdaQueryWrapper<OrderInfo>()
                .eq(OrderInfo::getOrderNo, orderNo).last("LIMIT 1"));
        if (order == null || !order.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ORDER_NOT_FOUND);
        }
        if (!Constants.OrderStatus.COMPLETED.equals(order.getStatus())) {
            throw new BizException(ResultCode.REVIEW_ORDER_NOT_COMPLETED);
        }
        Long existed = reviewMapper.selectCount(new LambdaQueryWrapper<Review>()
                .eq(Review::getOrderId, order.getId()));
        if (existed != null && existed > 0) {
            throw new BizException(ResultCode.REVIEW_ALREADY);
        }
        Review review = new Review();
        review.setOrderId(order.getId());
        review.setUserId(userId);
        review.setMerchantId(order.getMerchantId());
        review.setRating(req.getRating());
        review.setContent(req.getContent());
        review.setImages(req.getImages());
        reviewMapper.insert(review);
        // 商家评分聚合(原子 SQL 表达式)
        merchantMapper.update(null, new LambdaUpdateWrapper<Merchant>()
                .eq(Merchant::getId, order.getMerchantId())
                .setSql("rating = (rating * rating_count + " + req.getRating() + ") / (rating_count + 1), "
                        + "rating_count = rating_count + 1"));
    }

    /** 商家评价列表(分页)。 */
    public PageResult<ReviewVO> pageByMerchant(Long merchantId, PageQuery pq) {
        Page<Review> page = new Page<>(pq.getPage(), pq.getSize());
        Page<Review> result = reviewMapper.selectPage(page, new LambdaQueryWrapper<Review>()
                .eq(Review::getMerchantId, merchantId)
                .orderByDesc(Review::getId));
        List<ReviewVO> vos = result.getRecords().stream().map(r -> {
            ReviewVO vo = toVO(r);
            SysUser u = sysUserMapper.selectById(r.getUserId());
            if (u != null) {
                vo.setUserName(u.getNickname() != null ? u.getNickname() : u.getUsername());
            }
            OrderInfo o = orderInfoMapper.selectById(r.getOrderId());
            if (o != null) {
                vo.setOrderNo(o.getOrderNo());
            }
            return vo;
        }).collect(Collectors.toList());
        return PageResult.of(vos, result.getTotal(), pq.getSize(), pq.getPage());
    }

    /** 商家回复评价。 */
    @Transactional
    public void reply(Long merchantId, Long reviewId, ReviewReplyReq req) {
        Review review = reviewMapper.selectById(reviewId);
        if (review == null || !review.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.REVIEW_ALREADY);
        }
        Review upd = new Review();
        upd.setId(reviewId);
        upd.setReply(req.getReply());
        upd.setMerchantRepliedAt(LocalDateTime.now());
        reviewMapper.updateById(upd);
    }

    public static ReviewVO toVO(Review r) {
        ReviewVO vo = new ReviewVO();
        vo.setId(r.getId());
        vo.setOrderId(r.getOrderId());
        vo.setRating(r.getRating());
        vo.setContent(r.getContent());
        vo.setImages(r.getImages());
        vo.setReply(r.getReply());
        vo.setCreatedAt(r.getCreatedAt());
        return vo;
    }
}
