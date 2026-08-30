package com.campus.common.auth;

/**
 * 当前登录用户上下文(ThreadLocal)。由 web 层拦截器写入,Service 层读取。
 */
public final class UserContext {

    private static final ThreadLocal<UserInfo> HOLDER = new ThreadLocal<>();

    private UserContext() {
    }

    public static void set(UserInfo info) {
        HOLDER.set(info);
    }

    public static UserInfo get() {
        return HOLDER.get();
    }

    public static Long uid() {
        UserInfo info = HOLDER.get();
        return info == null ? null : info.getUserId();
    }

    public static String role() {
        UserInfo info = HOLDER.get();
        return info == null ? null : info.getRole();
    }

    public static void clear() {
        HOLDER.remove();
    }

    /** 登录用户信息。 */
    public static final class UserInfo {
        private final Long userId;
        private final String username;
        private final String role;

        public UserInfo(Long userId, String username, String role) {
            this.userId = userId;
            this.username = username;
            this.role = role;
        }

        public Long getUserId() {
            return userId;
        }

        public String getUsername() {
            return username;
        }

        public String getRole() {
            return role;
        }
    }
}
