"""数据契约历史明细：记录契约 Schema 的每次生效变更，承接发布与回滚回写。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DataContractHistoryService(BaseModuleService):
    MODULE = "data_contract_history"
    ENTITY = "契约历史明细"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "历史编号", "契约编号", "数据集", "变更类型", "变更前Schema",
        "变更后Schema", "关联批次", "关联回滚", "变更回执", "生效状态", "变更说明", "变更时间",
    ]
    REQUIRED_FIELDS = ["历史编号", "契约编号", "变更类型"]
    STATUS_ORDER = ["已生效", "未生效"]
    ACTION_RULES = {"确认生效": "已生效", "标记未生效": "未生效"}
    NEGATIVE_ACTIONS = []
