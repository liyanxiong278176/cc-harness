"""cc_harness.cli — CLI 入口层(spec 组件 8)。

公开 API 将在 Task 6 完整接入 main.py argparse 时,在三个子模块
(init/todo/resume)全部落地后,统一从 `__init__.py` 导出。

当前 __init__.py 保持空,避免子模块未到位时外部 import 失败。
具体使用:`from cc_harness.cli._shared import ...` / `from cc_harness.cli.init import ...`
"""
