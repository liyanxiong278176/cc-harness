package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.campus.common.api.PageResult;
import com.campus.common.api.ResultCode;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.common.model.PageQuery;
import com.campus.common.util.JsonUtils;
import com.campus.dao.entity.Dish;
import com.campus.dao.entity.DishCategory;
import com.campus.dao.entity.Merchant;
import com.campus.dao.entity.MerchantEmployee;
import com.campus.dao.entity.StockChangeLog;
import com.campus.dao.mapper.DishCategoryMapper;
import com.campus.dao.mapper.DishMapper;
import com.campus.dao.mapper.MerchantEmployeeMapper;
import com.campus.dao.mapper.MerchantMapper;
import com.campus.dao.mapper.StockChangeLogMapper;
import com.campus.service.dto.DishReq;
import com.campus.service.dto.DishStatusReq;
import com.campus.service.dto.StockReq;
import com.campus.service.support.CacheClient;
import com.campus.service.vo.DishVO;
import com.campus.service.vo.MenuVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * 菜品服务: 商家菜单查询(Redis 缓存)与商家端菜品/库存/上下架管理。
 */
@Service
public class DishService {

    private final DishMapper dishMapper;
    private final DishCategoryMapper categoryMapper;
    private final MerchantEmployeeMapper employeeMapper;
    private final MerchantMapper merchantMapper;
    private final StockChangeLogMapper stockChangeLogMapper;
    private final CacheClient cacheClient;

    public DishService(DishMapper dishMapper,
                       DishCategoryMapper categoryMapper,
                       MerchantEmployeeMapper employeeMapper,
                       MerchantMapper merchantMapper,
                       StockChangeLogMapper stockChangeLogMapper,
                       CacheClient cacheClient) {
        this.dishMapper = dishMapper;
        this.categoryMapper = categoryMapper;
        this.employeeMapper = employeeMapper;
        this.merchantMapper = merchantMapper;
        this.stockChangeLogMapper = stockChangeLogMapper;
        this.cacheClient = cacheClient;
    }

    // ---------- 用户端菜单(缓存) ----------

    public MenuVO menu(Long merchantId) {
        String key = Constants.RedisKeys.CACHE_MENU + merchantId;
        String cached = cacheClient.get(key);
        if (cached != null) {
            return JsonUtils.parse(cached, MenuVO.class);
        }
        MenuVO vo = buildMenu(merchantId);
        cacheClient.set(key, JsonUtils.toJson(vo), 300);
        return vo;
    }

    public void evictMenu(Long merchantId) {
        cacheClient.del(Constants.RedisKeys.CACHE_MENU + merchantId);
    }

    private MenuVO buildMenu(Long merchantId) {
        Merchant m = merchantMapper.selectById(merchantId);
        if (m == null) {
            throw new BizException(ResultCode.MERCHANT_NOT_FOUND);
        }
        MenuVO vo = new MenuVO();
        vo.setMerchant(MerchantService.toVO(m));
        List<DishCategory> cats = categoryMapper.selectList(new LambdaQueryWrapper<DishCategory>()
                .eq(DishCategory::getMerchantId, merchantId)
                .orderByAsc(DishCategory::getSortOrder));
        List<Dish> dishes = dishMapper.selectList(new LambdaQueryWrapper<Dish>()
                .eq(Dish::getMerchantId, merchantId)
                .eq(Dish::getStatus, 1));
        Map<Long, List<Dish>> byCat = dishes.stream().collect(Collectors.groupingBy(Dish::getCategoryId));
        List<MenuVO.CategoryMenu> menus = new ArrayList<>();
        for (DishCategory c : cats) {
            MenuVO.CategoryMenu cm = new MenuVO.CategoryMenu();
            cm.setId(c.getId());
            cm.setName(c.getName());
            cm.setDishes(byCat.getOrDefault(c.getId(), new ArrayList<>()).stream()
                    .map(DishService::toVO).collect(Collectors.toList()));
            menus.add(cm);
        }
        vo.setCategories(menus);
        return vo;
    }

    // ---------- 商家端 ----------

    private MerchantEmployee requireEmployee(Long userId) {
        MerchantEmployee emp = employeeMapper.selectOne(new LambdaQueryWrapper<MerchantEmployee>()
                .eq(MerchantEmployee::getUserId, userId).last("LIMIT 1"));
        if (emp == null) {
            throw new BizException(ResultCode.MERCHANT_NO_PERMISSION);
        }
        return emp;
    }

    public PageResult<DishVO> pageDishes(Long userId, Long categoryId, Integer status, PageQuery pq) {
        MerchantEmployee emp = requireEmployee(userId);
        Page<Dish> page = new Page<>(pq.getPage(), pq.getSize());
        LambdaQueryWrapper<Dish> qw = new LambdaQueryWrapper<Dish>()
                .eq(Dish::getMerchantId, emp.getMerchantId());
        if (categoryId != null) {
            qw.eq(Dish::getCategoryId, categoryId);
        }
        if (status != null) {
            qw.eq(Dish::getStatus, status);
        }
        qw.orderByDesc(Dish::getId);
        Page<Dish> result = dishMapper.selectPage(page, qw);
        return PageResult.of(result.getRecords().stream()
                .map(DishService::toVO).collect(Collectors.toList()),
                result.getTotal(), pq.getSize(), pq.getPage());
    }

    @Transactional
    public DishVO addDish(Long userId, DishReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        checkCategoryOwned(emp.getMerchantId(), req.getCategoryId());
        Dish dish = new Dish();
        dish.setMerchantId(emp.getMerchantId());
        dish.setCategoryId(req.getCategoryId());
        dish.setName(req.getName().trim());
        dish.setDescription(req.getDescription());
        dish.setImage(req.getImage());
        dish.setPrice(req.getPrice());
        dish.setOriginalPrice(req.getOriginalPrice());
        dish.setStock(req.getStock() == null ? 0 : req.getStock());
        dish.setStatus(1);
        dishMapper.insert(dish);
        evictMenu(emp.getMerchantId());
        return toVO(dish);
    }

    @Transactional
    public DishVO updateDish(Long userId, Long dishId, DishReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        Dish exist = requireDishOwned(emp.getMerchantId(), dishId);
        checkCategoryOwned(emp.getMerchantId(), req.getCategoryId());
        Dish upd = new Dish();
        upd.setId(dishId);
        upd.setCategoryId(req.getCategoryId());
        upd.setName(req.getName().trim());
        upd.setDescription(req.getDescription());
        upd.setImage(req.getImage());
        upd.setPrice(req.getPrice());
        upd.setOriginalPrice(req.getOriginalPrice());
        dishMapper.updateById(upd);
        evictMenu(emp.getMerchantId());
        return toVO(dishMapper.selectById(dishId));
    }

    @Transactional
    public void setStock(Long userId, Long dishId, StockReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        Dish exist = requireDishOwned(emp.getMerchantId(), dishId);
        Dish upd = new Dish();
        upd.setId(dishId);
        upd.setStock(req.getStock());
        dishMapper.updateById(upd);
        recordStockChange(dishId, null, Constants.StockChangeType.RESTOCK,
                req.getStock() - exist.getStock(), exist.getStock(), req.getStock());
        evictMenu(emp.getMerchantId());
    }

    @Transactional
    public void setStatus(Long userId, Long dishId, DishStatusReq req) {
        MerchantEmployee emp = requireEmployee(userId);
        requireDishOwned(emp.getMerchantId(), dishId);
        Dish upd = new Dish();
        upd.setId(dishId);
        upd.setStatus(req.getStatus() == null || req.getStatus() != 1 ? 0 : 1);
        dishMapper.updateById(upd);
        evictMenu(emp.getMerchantId());
    }

    public Dish requireDish(Long dishId) {
        Dish d = dishMapper.selectById(dishId);
        if (d == null) {
            throw new BizException(ResultCode.DISH_NOT_FOUND);
        }
        return d;
    }

    private Dish requireDishOwned(Long merchantId, Long dishId) {
        Dish d = dishMapper.selectById(dishId);
        if (d == null || !d.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.DISH_NOT_FOUND);
        }
        return d;
    }

    private void checkCategoryOwned(Long merchantId, Long categoryId) {
        DishCategory c = categoryMapper.selectById(categoryId);
        if (c == null || !c.getMerchantId().equals(merchantId)) {
            throw new BizException(ResultCode.CATEGORY_NOT_FOUND);
        }
    }

    private void recordStockChange(Long dishId, Long orderId, String type,
                                   int changeQty, int beforeStock, int afterStock) {
        StockChangeLog log = new StockChangeLog();
        log.setDishId(dishId);
        log.setOrderId(orderId);
        log.setChangeType(type);
        log.setChangeQty(changeQty);
        log.setBeforeStock(beforeStock);
        log.setAfterStock(afterStock);
        stockChangeLogMapper.insert(log);
    }

    public static DishVO toVO(Dish d) {
        DishVO vo = new DishVO();
        vo.setId(d.getId());
        vo.setMerchantId(d.getMerchantId());
        vo.setCategoryId(d.getCategoryId());
        vo.setSkuCode(d.getSkuCode());
        vo.setName(d.getName());
        vo.setDescription(d.getDescription());
        vo.setImage(d.getImage());
        vo.setPrice(d.getPrice());
        vo.setOriginalPrice(d.getOriginalPrice());
        vo.setStock(d.getStock());
        vo.setSoldCount(d.getSoldCount());
        vo.setStatus(d.getStatus());
        return vo;
    }
}
