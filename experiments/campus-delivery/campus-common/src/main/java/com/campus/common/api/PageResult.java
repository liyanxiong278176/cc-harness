package com.campus.common.api;

import lombok.Data;

import java.io.Serializable;
import java.util.List;

/**
 * 统一分页结果。
 */
@Data
public class PageResult<T> implements Serializable {

    private static final long serialVersionUID = 1L;

    private List<T> records;
    private long total;
    private long size;
    private long current;

    public static <T> PageResult<T> of(List<T> records, long total, long size, long current) {
        PageResult<T> p = new PageResult<>();
        p.records = records;
        p.total = total;
        p.size = size;
        p.current = current;
        return p;
    }
}
