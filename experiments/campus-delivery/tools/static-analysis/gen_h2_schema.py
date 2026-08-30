#!/usr/bin/env python3
"""将 db/init/01-schema.sql 转换为 H2(MySQL 模式)可执行的测试建表脚本。
处理:
  - 去掉 MySQL 专属会话语句(SET NAMES / SET FOREIGN_KEY_CHECKS)
  - 去掉表级选项(ENGINE/CHARSET/COLLATE/COMMENT,包括与 ')' 同行的情况)
  - 去掉列级 COMMENT 与 ON UPDATE CURRENT_TIMESTAMP
  - 保留表/列/主键/唯一键/索引(反引号、AUTO_INCREMENT、TINYINT 在 H2 MySQL 模式可用)
输出: campus-web/src/test/resources/h2-schema.sql
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "db" / "init" / "01-schema.sql"
OUT = ROOT / "campus-web" / "src" / "test" / "resources" / "h2-schema.sql"


def main():
    if not SRC.exists():
        print(f"缺少 {SRC}", file=sys.stderr)
        return 1
    out_lines = []
    for raw in SRC.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            out_lines.append("")
            continue
        if re.match(r"SET\s+(NAMES|FOREIGN_KEY_CHECKS)", s, re.I):
            continue
        s = re.sub(r"\s+COMMENT\s+'[^']*'", "", s, flags=re.I)
        s = re.sub(r"\s+ON\s+UPDATE\s+CURRENT_TIMESTAMP", "", s, flags=re.I)
        s = re.sub(r"\)\s*ENGINE[^,;]*?(?=;|$)", ")", s, flags=re.I)
        if re.match(r"(ENGINE|DEFAULT CHARSET|COLLATE|COMMENT)\s*=", s, re.I) \
                or re.match(r"^COMMENT\s+'", s, re.I):
            continue
        out_lines.append(s)
    body = "\n".join(out_lines)
    body = re.sub(r",\s*\n\s*\)", "\n)", body)
    body = body + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body, encoding="utf-8")
    print(f"生成 {OUT.relative_to(ROOT)} ({len(out_lines)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
