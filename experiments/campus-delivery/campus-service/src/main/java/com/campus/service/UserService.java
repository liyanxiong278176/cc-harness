package com.campus.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.common.api.ResultCode;
import com.campus.common.exception.BizException;
import com.campus.common.util.AesUtils;
import com.campus.dao.entity.SysUser;
import com.campus.dao.entity.UserAddress;
import com.campus.dao.mapper.SysUserMapper;
import com.campus.dao.mapper.UserAddressMapper;
import com.campus.service.dto.AddressReq;
import com.campus.service.dto.ProfileUpdateReq;
import com.campus.service.vo.AddressVO;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 用户服务: 个人信息与收货地址 CRUD。
 */
@Service
public class UserService {

    private final SysUserMapper sysUserMapper;
    private final UserAddressMapper addressMapper;

    public UserService(SysUserMapper sysUserMapper, UserAddressMapper addressMapper) {
        this.sysUserMapper = sysUserMapper;
        this.addressMapper = addressMapper;
    }

    @Transactional
    public void updateProfile(Long userId, ProfileUpdateReq req) {
        SysUser upd = new SysUser();
        upd.setId(userId);
        if (StringUtils.hasText(req.getNickname())) {
            upd.setNickname(req.getNickname().trim());
        }
        if (StringUtils.hasText(req.getAvatar())) {
            upd.setAvatar(req.getAvatar().trim());
        }
        if (StringUtils.hasText(req.getPhone())) {
            upd.setPhone(AesUtils.encrypt(req.getPhone().trim()));
        }
        sysUserMapper.updateById(upd);
    }

    // ---------------- 地址 ----------------

    public List<AddressVO> listAddresses(Long userId) {
        List<UserAddress> list = addressMapper.selectList(new LambdaQueryWrapper<UserAddress>()
                .eq(UserAddress::getUserId, userId)
                .orderByDesc(UserAddress::getIsDefault)
                .orderByDesc(UserAddress::getId));
        return list.stream().map(UserService::toVO).collect(Collectors.toList());
    }

    @Transactional
    public AddressVO addAddress(Long userId, AddressReq req) {
        UserAddress addr = new UserAddress();
        addr.setUserId(userId);
        addr.setReceiverName(req.getReceiverName().trim());
        addr.setReceiverPhone(AesUtils.encrypt(req.getReceiverPhone().trim()));
        addr.setCampusZone(req.getCampusZone().trim());
        addr.setDetail(req.getDetail().trim());
        int def = req.getIsDefault() == null ? 0 : req.getIsDefault();
        addr.setIsDefault(def);
        if (def == 1) {
            clearDefault(userId);
        }
        addressMapper.insert(addr);
        return toVO(addr);
    }

    @Transactional
    public AddressVO updateAddress(Long userId, Long id, AddressReq req) {
        UserAddress exist = requireMine(userId, id);
        UserAddress upd = new UserAddress();
        upd.setId(id);
        upd.setReceiverName(req.getReceiverName().trim());
        upd.setReceiverPhone(AesUtils.encrypt(req.getReceiverPhone().trim()));
        upd.setCampusZone(req.getCampusZone().trim());
        upd.setDetail(req.getDetail().trim());
        int def = req.getIsDefault() == null ? exist.getIsDefault() : req.getIsDefault();
        upd.setIsDefault(def);
        if (def == 1) {
            clearDefault(userId);
        }
        addressMapper.updateById(upd);
        return toVO(addressMapper.selectById(id));
    }

    @Transactional
    public void deleteAddress(Long userId, Long id) {
        UserAddress exist = requireMine(userId, id);
        addressMapper.deleteById(exist.getId());
    }

    private void clearDefault(Long userId) {
        addressMapper.clearDefault(userId);
    }

    private UserAddress requireMine(Long userId, Long id) {
        UserAddress addr = addressMapper.selectById(id);
        if (addr == null || !addr.getUserId().equals(userId)) {
            throw new BizException(ResultCode.ADDRESS_NOT_OWNED);
        }
        return addr;
    }

    private static AddressVO toVO(UserAddress a) {
        AddressVO vo = new AddressVO();
        vo.setId(a.getId());
        vo.setReceiverName(a.getReceiverName());
        vo.setReceiverPhone(AesUtils.decrypt(a.getReceiverPhone()));
        vo.setCampusZone(a.getCampusZone());
        vo.setDetail(a.getDetail());
        vo.setIsDefault(a.getIsDefault());
        return vo;
    }
}
