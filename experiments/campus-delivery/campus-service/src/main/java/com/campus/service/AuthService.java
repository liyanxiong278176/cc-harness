package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.api.ResultCode;
import com.campus.common.auth.JwtUtils;
import com.campus.common.constant.Constants;
import com.campus.common.exception.BizException;
import com.campus.common.util.AesUtils;
import com.campus.common.util.MaskUtils;
import com.campus.common.util.PasswordUtils;
import com.campus.dao.entity.SysUser;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.service.dto.LoginReq;
import com.campus.service.dto.PasswordChangeReq;
import com.campus.service.dto.RegisterReq;
import com.campus.service.support.UserConverter;
import com.campus.service.vo.LoginVO;
import com.campus.service.vo.UserVO;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;

/**
 * 认证与账号服务: 注册 / 登录 / 当前用户 / 改密。
 */
@Service
public class AuthService {

    private static final Logger log = LoggerFactory.getLogger(AuthService.class);

    private final SysUserMapper sysUserMapper;

    public AuthService(SysUserMapper sysUserMapper) {
        this.sysUserMapper = sysUserMapper;
    }

    @Transactional
    public LoginVO register(RegisterReq req) {
        Long exist = sysUserMapper.selectCount(new LambdaQueryWrapper<SysUser>()
                .eq(SysUser::getUsername, req.getUsername()));
        if (exist != null && exist > 0) {
            throw new BizException(ResultCode.USERNAME_EXISTS);
        }
        SysUser user = new SysUser();
        user.setUsername(req.getUsername().trim());
        user.setPasswordHash(PasswordUtils.encode(req.getPassword()));
        user.setNickname(StringUtils.hasText(req.getNickname()) ? req.getNickname().trim() : req.getUsername());
        user.setRole(Constants.UserRole.USER);
        user.setStatus(1);
        if (StringUtils.hasText(req.getPhone())) {
            user.setPhone(AesUtils.encrypt(req.getPhone().trim()));
        }
        sysUserMapper.insert(user);
        return buildLoginVO(user);
    }

    public LoginVO login(LoginReq req) {
        SysUser user = sysUserMapper.selectOne(new LambdaQueryWrapper<SysUser>()
                .eq(SysUser::getUsername, req.getUsername().trim()));
        if (user == null || !PasswordUtils.matches(req.getPassword(), user.getPasswordHash())) {
            throw new BizException(ResultCode.LOGIN_FAILED);
        }
        if (user.getStatus() == null || user.getStatus() != 1) {
            throw new BizException(ResultCode.ACCOUNT_DISABLED);
        }
        SysUser upd = new SysUser();
        upd.setId(user.getId());
        upd.setLastLoginAt(LocalDateTime.now());
        sysUserMapper.updateById(upd);
        log.info("user login: id={} role={}", user.getId(), user.getRole());
        return buildLoginVO(user);
    }

    public UserVO me(Long userId) {
        SysUser user = sysUserMapper.selectById(userId);
        if (user == null) {
            throw new BizException(ResultCode.USER_NOT_FOUND);
        }
        return UserConverter.toVO(user);
    }

    @Transactional
    public void changePassword(Long userId, PasswordChangeReq req) {
        SysUser user = sysUserMapper.selectById(userId);
        if (user == null) {
            throw new BizException(ResultCode.USER_NOT_FOUND);
        }
        if (!PasswordUtils.matches(req.getOldPassword(), user.getPasswordHash())) {
            throw new BizException(ResultCode.OLD_PASSWORD_WRONG);
        }
        SysUser upd = new SysUser();
        upd.setId(userId);
        upd.setPasswordHash(PasswordUtils.encode(req.getNewPassword()));
        sysUserMapper.updateById(upd);
    }

    private LoginVO buildLoginVO(SysUser user) {
        UserVO vo = UserConverter.toVO(user);
        LoginVO login = new LoginVO();
        login.setUser(vo);
        login.setToken(JwtUtils.createToken(user.getId(), user.getUsername(), user.getRole()));
        return login;
    }
}
