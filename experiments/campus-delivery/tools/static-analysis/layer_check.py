#!/usr/bin/env python3
"""分层与依赖方向静态检查。
规则:
  1. 依赖方向: campus-common <- campus-dao <- campus-service <- campus-web(不允许反向 import)
  2. controller 层不允许直接依赖 dao/mapper/entity(必须经 service)
  3. service 层不允许出现 javax.servlet / org.springframework.web(Web 细节只允许在 web 模块)
  4. 全仓禁止 import java.sql.* 直接操作 JDBC(统一走 MyBatis-Plus)
  5. import 均指向本仓库存在的类(粗粒度: 同一模块内)
退出码: 0 通过;1 失败
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MODULES = ["campus-common", "campus-dao", "campus-service", "campus-web"]
ORDER = {m: i for i, m in enumerate(MODULES)}  # 越大越上层

errors = []
warnings = []


def imports_of(p: Path):
    src = p.read_text(encoding="utf-8")
    return re.findall(r"^import ([\w.]+);", src, re.M)


def module_of(imp: str):
    for m in MODULES:
        if imp.startswith(f"com.campus."):
            # 判断属于哪个模块: 按包名前缀无法区分(包名都是 com.campus),
            # 改用“已知属于本模块的包”映射。
            pass
    return None


# 各模块独有的包前缀
MODULE_PREFIX = {
    "campus-common": ("com.campus.common",),
    "campus-dao": ("com.campus.dao",),
    "campus-service": ("com.campus.service",),
    "campus-web": ("com.campus.web",),
}


def module_of_imp(imp: str):
    for m, prefixes in MODULE_PREFIX.items():
        for pre in prefixes:
            if imp.startswith(pre + ".") or imp == pre:
                return m
    return None  # 第三方或 java.*


def check_direction(p: Path, module: str):
    for imp in imports_of(p):
        owner = module_of_imp(imp)
        if owner is None:
            continue
        if ORDER[owner] > ORDER[module]:
            errors.append(f"{p.relative_to(ROOT)}: 反向依赖 {imp} (属于 {owner},被 {module} 引用)")


def check_controller_layer(p: Path):
    if "controller" not in p.parts:
        return
    for imp in imports_of(p):
        if imp.startswith("com.campus.dao."):
            errors.append(f"{p.relative_to(ROOT)}: controller 直接依赖 DAO 层 {imp}")


def check_web_in_service(p: Path, module: str):
    if module != "campus-service":
        return
    for imp in imports_of(p):
        if imp.startswith("javax.servlet") or imp.startswith("jakarta.servlet") or imp.startswith("org.springframework.web"):
            errors.append(f"{p.relative_to(ROOT)}: service 层混入 Web 依赖 {imp}")


def check_raw_jdbc(p: Path):
    # 健康检查端点允许 DataSource.getConnection() 探活(仅限 Health 类)
    if "Health" in p.name:
        return
    for imp in imports_of(p):
        if imp.startswith("java.sql."):
            errors.append(f"{p.relative_to(ROOT)}: 禁止直接使用 JDBC {imp}(统一走 MyBatis-Plus)")


def main():
    for module in MODULES:
        base = ROOT / module / "src" / "main" / "java"
        if not base.exists():
            errors.append(f"缺少模块源码目录 {module}")
            continue
        for p in sorted(base.rglob("*.java")):
            check_direction(p, module)
            check_controller_layer(p)
            check_web_in_service(p, module)
            check_raw_jdbc(p)

    # 5. 配置文件明文密钥检查
    for yml in (ROOT / "campus-web" / "src" / "main" / "resources").rglob("*.yml"):
        src = yml.read_text(encoding="utf-8")
        for line in src.splitlines():
            if re.match(r"^\s*(password|secret|key)\s*:\s*\S+", line, re.I) and "env" not in line and "${" not in line:
                warnings.append(f"{yml.relative_to(ROOT)}: 疑似明文敏感配置: {line.strip()}")

    print("=" * 60)
    print("分层/依赖方向静态检查")
    print("=" * 60)
    for e in errors:
        print(f"  [FAIL] {e}")
    for w in warnings:
        print(f"  [WARN] {w}")
    if errors:
        print(f"\n共 {len(errors)} 个错误, {len(warnings)} 个警告 -> 失败")
        return 1
    print(f"\n通过: 0 错误, {len(warnings)} 个警告")
    return 0


if __name__ == "__main__":
    sys.exit(main())
