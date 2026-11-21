"""集市领取文件领域服务：记录下游领取的确认版本，冻结字段快照供回滚后对账。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.mart_release_common import (
    batch_state,
    claim_columns,
    find_batch,
    find_mart,
    normalize_columns,
)
from app.store import store


class MartReleaseClaimService(BaseModuleService):
    MODULE = "mart_release_claim"
    ENTITY = "集市领取文件"
    OPERATION_TERM = "领取"
    LIST_FIELDS = [
        "领取编号", "集市编号", "批次编号", "文件名称", "确认版本",
        "领取人", "领取时间", "领取状态",
    ]
    REQUIRED_FIELDS = ["领取编号", "集市编号", "批次编号", "文件名称"]
    STATUS_ORDER = ["待确认", "已领取", "已对账", "已失效"]
    ACTION_RULES = {"确认领取": "已领取", "标记对账": "已对账", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = ["标记失效"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        mart = find_mart(values.get("集市编号"))
        if mart is None:
            return None, ["集市编号(未找到对应结果集市)"]
        batch = find_batch(values.get("批次编号"))
        if batch is None:
            return None, ["批次编号(未找到对应发布批次)"]
        if str(batch.get("集市编号", "")) != str(mart.get("集市编号", "")):
            return None, ["批次编号(不属于该结果集市)"]
        if batch_state(batch) not in {"待发布", "已发布", "已下架"}:
            return None, ["批次编号(批次尚未形成可领取的发布版本)"]

        # 领取即冻结确认版本；后续字段改名或回滚都不会改写这份快照。
        confirmed = normalize_columns(values.get("确认版本")) or normalize_columns(batch.get("字段定义"))
        if not confirmed:
            return None, ["确认版本(批次没有可用字段)"]

        rows = store.rows(self.MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "领取编号": str(values.get("领取编号")).strip(),
            "集市编号": mart.get("集市编号", ""),
            "批次编号": batch.get("批次编号", ""),
            "文件名称": str(values.get("文件名称")).strip(),
            "确认版本": ",".join(confirmed),
            "领取人": str(values.get("领取人") or "dashboard"),
            "领取时间": str(values.get("领取时间") or time.strftime("%Y-%m-%d %H:%M:%S")),
            "领取状态": "待确认",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "字段快照": ",".join(confirmed),
            "_mart_id": mart.get("id"),
            "_batch_id": batch.get("id"),
        }
        rows.append(entry)

        batch["领取文件数"] = str(int(batch.get("领取文件数") or 0) + 1)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        action = action.strip()
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if action == "确认领取":
            frozen = claim_columns(entry)
            batch = find_batch(entry.get("批次编号"))
            if not frozen:
                return None, "领取记录缺少确认版本，不能确认领取"
            if batch is not None and batch_state(batch) == "草稿":
                return None, "批次尚未发布，不能确认领取"
        entry["领取状态"] = self.ACTION_RULES[action]
        entry["status"] = self.ACTION_RULES[action]
        entry["pending"] = action != "标记对账"
        entry["abnormal"] = action == "标记失效"
        return entry, f"{self.ENTITY}已{action}"
