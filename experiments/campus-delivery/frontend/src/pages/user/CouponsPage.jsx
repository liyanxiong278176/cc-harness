// 我的优惠券: 状态筛选
import { useCallback, useEffect, useState } from 'react';
import { Card, Table, Tabs, Tag, message, Empty } from 'antd';
import { userApi } from '../../api/user.js';
import {
  formatMoney, formatDateTime, couponTypeText, couponStatusText,
} from '../../utils/format.js';

const STATUS_TABS = [
  { key: 'UNUSED', label: '未使用' },
  { key: 'USED', label: '已使用' },
  { key: 'EXPIRED', label: '已过期' },
];

export default function CouponsPage() {
  const [status, setStatus] = useState('UNUSED');
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (s = status) => {
    setLoading(true);
    try {
      const res = await userApi.myCoupons({ status: s });
      setList(res || []);
    } catch (e) {
      message.error(e.message || '加载优惠券失败');
      setList([]);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  return (
    <div>
      <Tabs
        activeKey={status}
        onChange={setStatus}
        items={STATUS_TABS}
      />
      <Table
        rowKey="id"
        loading={loading}
        dataSource={list}
        locale={{ emptyText: <Empty description="暂无优惠券" /> }}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          {
            title: '类型',
            dataIndex: 'type',
            width: 110,
            render: (v) => <Tag>{couponTypeText(v)}</Tag>,
          },
          {
            title: '优惠',
            key: 'discount',
            width: 160,
            render: (_, c) => (
              c.type === 'FULL_REDUCTION'
                ? <span>满{formatMoney(c.thresholdAmount)}减{formatMoney(c.discountAmount)}</span>
                : <span>满{formatMoney(c.thresholdAmount)}打{c.discountRate}折</span>
            ),
          },
          { title: '状态', dataIndex: 'status', width: 100, render: (v) => couponStatusText(v) },
          { title: '有效期至', dataIndex: 'expireAt', width: 160, render: (v) => formatDateTime(v) },
        ]}
      />
    </div>
  );
}
