// 路由总表: 用户端 / 商家端 / 骑手端 同仓分区
// 用户端 /user 相关与商家端 /merchant 路由均需登录并按角色守卫
import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Spin } from 'antd';
import { RequireAuth } from './components/RequireAuth.jsx';
import UserLayout from './layouts/UserLayout.jsx';
import MerchantLayout from './layouts/MerchantLayout.jsx';
import RiderLayout from './layouts/RiderLayout.jsx';

// ---------- 公开页 ----------
const LoginPage = lazy(() => import('./pages/LoginPage.jsx'));
const RegisterPage = lazy(() => import('./pages/RegisterPage.jsx'));

// ---------- 用户端 ----------
const UserHomePage = lazy(() => import('./pages/user/HomePage.jsx'));
const MerchantDetailPage = lazy(() => import('./pages/user/MerchantDetailPage.jsx'));
const CartPage = lazy(() => import('./pages/user/CartPage.jsx'));
const CheckoutPage = lazy(() => import('./pages/user/CheckoutPage.jsx'));
const OrdersPage = lazy(() => import('./pages/user/OrdersPage.jsx'));
const OrderDetailPage = lazy(() => import('./pages/user/OrderDetailPage.jsx'));
const CouponsPage = lazy(() => import('./pages/user/CouponsPage.jsx'));
const AddressesPage = lazy(() => import('./pages/user/AddressesPage.jsx'));
const NotificationsPage = lazy(() => import('./pages/user/NotificationsPage.jsx'));
const ProfilePage = lazy(() => import('./pages/user/ProfilePage.jsx'));

// ---------- 商家端 ----------
const MerchantDashboard = lazy(() => import('./pages/merchant/DashboardPage.jsx'));
const MerchantProfilePage = lazy(() => import('./pages/merchant/ProfilePage.jsx'));
const CategoriesPage = lazy(() => import('./pages/merchant/CategoriesPage.jsx'));
const DishesPage = lazy(() => import('./pages/merchant/DishesPage.jsx'));
const MerchantOrdersPage = lazy(() => import('./pages/merchant/OrdersPage.jsx'));
const ReviewsPage = lazy(() => import('./pages/merchant/ReviewsPage.jsx'));
const RefundsPage = lazy(() => import('./pages/merchant/RefundsPage.jsx'));

// ---------- 骑手端 ----------
const RiderTasksPage = lazy(() => import('./pages/rider/TasksPage.jsx'));
const RiderAvailablePage = lazy(() => import('./pages/rider/AvailablePage.jsx'));

function PageFallback() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 120 }}>
      <Spin size="large" />
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        {/* 公开 */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />

        {/* 用户端 */}
        <Route
          element={(
            <RequireAuth allowRoles={['USER']}>
              <UserLayout />
            </RequireAuth>
          )}
        >
          <Route path="/" element={<UserHomePage />} />
          <Route path="/merchants/:id" element={<MerchantDetailPage />} />
          <Route path="/cart" element={<CartPage />} />
          <Route path="/checkout" element={<CheckoutPage />} />
          <Route path="/orders" element={<OrdersPage />} />
          <Route path="/orders/:orderNo" element={<OrderDetailPage />} />
          <Route path="/coupons" element={<CouponsPage />} />
          <Route path="/addresses" element={<AddressesPage />} />
          <Route path="/notifications" element={<NotificationsPage />} />
          <Route path="/profile" element={<ProfilePage />} />
        </Route>

        {/* 商家端 */}
        <Route
          element={(
            <RequireAuth allowRoles={['MERCHANT']}>
              <MerchantLayout />
            </RequireAuth>
          )}
        >
          <Route path="/merchant" element={<MerchantDashboard />} />
          <Route path="/merchant/profile" element={<MerchantProfilePage />} />
          <Route path="/merchant/categories" element={<CategoriesPage />} />
          <Route path="/merchant/dishes" element={<DishesPage />} />
          <Route path="/merchant/orders" element={<MerchantOrdersPage />} />
          <Route path="/merchant/reviews" element={<ReviewsPage />} />
          <Route path="/merchant/refunds" element={<RefundsPage />} />
        </Route>

        {/* 骑手端 */}
        <Route
          element={(
            <RequireAuth allowRoles={['RIDER']}>
              <RiderLayout />
            </RequireAuth>
          )}
        >
          <Route path="/rider" element={<RiderTasksPage />} />
          <Route path="/rider/available" element={<RiderAvailablePage />} />
        </Route>

        {/* 兜底 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
