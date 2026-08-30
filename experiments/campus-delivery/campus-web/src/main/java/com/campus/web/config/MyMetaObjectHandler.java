package com.campus.web.config;

import com.baomidou.mybatisplus.core.handlers.MetaObjectHandler;
import com.campus.common.auth.UserContext;
import org.apache.ibatis.reflection.MetaObject;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;

/**
 * 审计字段自动填充:
 * <ul>
 *   <li>insert: createdBy / updatedBy / createdAt / updatedAt;</li>
 *   <li>update: updatedBy / updatedAt。</li>
 * </ul>
 * 操作人取自 {@link UserContext#uid()}(由 JwtAuthInterceptor 写入),未登录场景(如定时任务)兜底为 0。
 * 实体字段需用 {@code @TableField(fill = FieldFill.INSERT/INSERT_UPDATE)} 声明(BaseEntity 统一提供)。
 */
@Component
public class MyMetaObjectHandler implements MetaObjectHandler {

    /** 系统/定时任务等无登录上下文的默认操作人。 */
    private static final Long SYSTEM_OPERATOR = 0L;

    @Override
    public void insertFill(MetaObject metaObject) {
        LocalDateTime now = LocalDateTime.now();
        Long uid = operator();
        this.strictInsertFill(metaObject, "createdBy", Long.class, uid);
        this.strictInsertFill(metaObject, "updatedBy", Long.class, uid);
        this.strictInsertFill(metaObject, "createdAt", LocalDateTime.class, now);
        this.strictInsertFill(metaObject, "updatedAt", LocalDateTime.class, now);
    }

    @Override
    public void updateFill(MetaObject metaObject) {
        this.strictUpdateFill(metaObject, "updatedBy", Long.class, operator());
        this.strictUpdateFill(metaObject, "updatedAt", LocalDateTime.class, LocalDateTime.now());
    }

    private static Long operator() {
        Long uid = UserContext.uid();
        return uid == null ? SYSTEM_OPERATOR : uid;
    }
}
