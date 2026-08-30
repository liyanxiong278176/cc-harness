package com.campus.service.vo;

import lombok.Data;

import java.util.List;

/** 商家菜单出参: 分类 + 上架菜品。 */
@Data
public class MenuVO {

    private MerchantVO merchant;

    private List<CategoryMenu> categories;

    @Data
    public static class CategoryMenu {
        private Long id;
        private String name;
        private List<DishVO> dishes;
    }
}
