"""Files route stub.

完整实现由 Task 13 填充(GET /api/sessions/{sid}/files 列工作区文件、
GET /api/sessions/{sid}/files/{path:path} 读文件内容,两种路径共用 /api/sessions 前缀)。
"""
from fastapi import APIRouter

router = APIRouter()
