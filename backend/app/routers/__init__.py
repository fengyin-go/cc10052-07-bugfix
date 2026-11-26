"""业务路由汇总：专用领域事务先注册，通用资源路由后注册。"""
from __future__ import annotations

from app.routers import domain_ops, resource

ROUTERS = [domain_ops, resource]
