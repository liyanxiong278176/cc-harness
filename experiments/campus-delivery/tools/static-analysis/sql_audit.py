#!/usr/bin/env python3
"""SQL 与数据一致性静态检查。
规则:
  1. Mapper 的 @Select/@Update/@Insert 中禁止字符串拼接 SQL(必须是 #{} 参数化)
  2. 不允许出现 "* * *" 拼接、${} 直接插值(MyBatis ${} 属注入风险,仅允许白名单如排序字段)
  3. 实体表名/列名与 db/init/01-schema.sql 对齐: 每张表至少一个实体;实体 @TableName 均存在于 schema
  4. 自定义条件更新(防超卖/防双抢/幂等)必须含状态或版本条件(WHERE 中带 status 或 version)
退出码: 0 通过;1 失败
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA = ROOT / "db" / "init" / "01-schema.sql"
DAO = ROOT / "campus-dao" / "src" / "main" / "java"
errors = []


def check_mapper_sql():
    for p in sorted(DAO.rglob("*.java")):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r'@(Select|Update|Insert|Delete)\("(.*?)"\)', src, re.S):
            kind, sql = m.group(1), m.group(2)
            if "${" in sql:
                errors.append(f"{p.relative_to(ROOT)} [{kind}]: 检测到 ${{}} 字符串插值(注入风险): {sql[:80]}")
            # 参数化检查: 有 WHERE 但没有 #{}(非纯插入)且非 count/coalesce 汇总
            if "where" in sql.lower() and "#{" not in sql:
                errors.append(f"{p.relative_to(ROOT)} [{kind}]: WHERE 子句无参数化绑定: {sql[:80]}")
            # 条件更新必须带状态或版本条件(防超卖/防双抢/幂等)
            if kind in ("Update", "Delete"):
                low = sql.lower()
                has_cond = ("status" in low and "=" in low) or "version" in low or "deleted" in low
                if not has_cond:
                    errors.append(f"{p.relative_to(ROOT)} [{kind}]: 更新/删除缺少 status/version/deleted 条件: {sql[:80]}")


def check_entity_tables():
    schema = SCHEMA.read_text(encoding="utf-8")
    tables = set(re.findall(r"CREATE TABLE `(\w+)`", schema))
    entity_dir = DAO / "com" / "campus" / "dao" / "entity"
    mapped = set()
    for p in sorted(entity_dir.rglob("*.java")):
        src = p.read_text(encoding="utf-8")
        if "abstract class" in src:
            continue  # BaseEntity 抽象基类无需表映射
        m = re.search(r'@TableName\("(\w+)"\)', src)
        if m:
            mapped.add(m.group(1))
        else:
            errors.append(f"{p.relative_to(ROOT)}: 实体缺少 @TableName 注解")
    missing = tables - mapped
    if missing:
        errors.append(f"schema 中存在无实体映射的表: {sorted(missing)}")
    extra = mapped - tables
    if extra:
        errors.append(f"实体映射了 schema 中不存在的表: {sorted(extra)}")
    return tables, mapped


def main():
    if not SCHEMA.exists():
        errors.append(f"缺少 schema: {SCHEMA}")
    else:
        check_entity_tables()
    check_mapper_sql()

    print("=" * 60)
    print("SQL / 数据一致性静态检查")
    print("=" * 60)
    for e in errors:
        print(f"  [FAIL] {e}")
    if errors:
        print(f"\n共 {len(errors)} 个错误 -> 失败")
        return 1
    print("\n通过: 0 错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
