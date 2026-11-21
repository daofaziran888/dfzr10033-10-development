"""集市发布批次领域服务：批次登记、确认、发布生效、下架与批次级回滚入口。

一个批次只允许携带一套字段定义；发布生效是原子的，切换失败时保留原生效批
及其完整快照，不会把两批字段拼成一份快照。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.mart_release_common import (
    batch_belongs_to_mart,
    batch_state,
    batches_for_mart,
    column_key,
    current_effective_batch,
    find_mart,
    inspect_columns,
    normalize_columns,
    previous_release_batch,
    resolve_contract,
    set_batch_state,
    write_contract_history,
)
from app.store import store


class MartReleaseBatchService(BaseModuleService):
    MODULE = "mart_release_batch"
    ENTITY = "集市发布批次"
    OPERATION_TERM = "发布"
    LIST_FIELDS = [
        "批次编号", "集市编号", "发布版本", "字段定义", "数据快照",
        "契约编号", "领取文件数", "发布状态", "发布时间",
    ]
    REQUIRED_FIELDS = ["批次编号", "集市编号", "字段定义"]
    STATUS_ORDER = ["草稿", "确认中", "待发布", "已发布", "已下架", "发布失败"]
    ACTION_RULES = {
        "提交确认": "确认中",
        "确认发布": "待发布",
        "发布生效": "已发布",
        "下架批次": "已下架",
        "标记失败": "发布失败",
        "回滚到上一发布": "已下架",
    }
    NEGATIVE_ACTIONS = ["标记失败"]

    _ALLOWED_FROM = {
        "提交确认": {"草稿"},
        "确认发布": {"确认中"},
        "发布生效": {"待发布"},
        "下架批次": {"已发布", "待发布"},
        "标记失败": {"草稿", "确认中", "待发布"},
        "回滚到上一发布": {"已发布"},
    }

    # ---------- 登记 ----------

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

        batch_no = str(values.get("批次编号")).strip()
        if any(str(row.get("批次编号", "")) == batch_no for row in store.rows(self.MODULE)):
            return None, [f"批次编号(已存在 {batch_no})"]

        inspected = inspect_columns(values.get("字段定义"))
        if inspected["empty"]:
            return None, ["字段定义(至少需要一个字段)"]
        if inspected["duplicates"]:
            return None, [f"字段定义(重复字段：{'、'.join(inspected['duplicates'])})"]
        columns = inspected["columns"]

        sibling = batches_for_mart(mart)
        sequence = max(
            (int(row.get("发布序号") or row.get("id") or 0) for row in sibling),
            default=0,
        ) + 1
        contract = resolve_contract(values.get("契约编号"), mart, *sibling)

        rows = store.rows(self.MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "批次编号": batch_no,
            "集市编号": mart.get("集市编号", ""),
            "发布版本": str(values.get("发布版本") or f"v{sequence}.0"),
            "字段定义": ",".join(columns),
            "数据快照": _snapshot_label(batch_no, columns),
            "契约编号": contract.get("契约编号", "") if contract else str(values.get("契约编号") or ""),
            "领取文件数": "0",
            "发布状态": "草稿",
            "发布时间": "",
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "发布序号": sequence,
            "字段指纹": _fingerprint(columns),
            "是否当前生效": False,
            "可承接查询": False,
            "_mart_id": mart.get("id"),
            "_contract_id": contract.get("id") if contract else None,
            "_snapshot_columns": columns,
        }
        rows.append(entry)
        return entry, []

    # ---------- 状态流转 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        action = action.strip()
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "回滚到上一发布":
            return self._rollback(entry)

        allowed = self._ALLOWED_FROM.get(action, set())
        if allowed and batch_state(entry) not in allowed:
            return None, (
                f"批次当前状态为「{batch_state(entry)}」，"
                f"不能执行「{action}」；允许的来源状态：{'、'.join(sorted(allowed))}"
            )

        if action == "提交确认":
            ok, message = self._enter_confirming(entry)
            if not ok:
                return None, message
        elif action == "发布生效":
            ok, message = self._publish(entry)
            if not ok:
                return None, message
        else:
            set_batch_state(entry, self.ACTION_RULES[action])
            if action == "标记失败":
                entry["abnormal"] = True
            if action == "下架批次":
                entry["是否当前生效"] = False
                entry["可承接查询"] = False
        return entry, f"{self.ENTITY}已{action}"

    def _enter_confirming(self, entry: dict[str, Any]) -> tuple[bool, str]:
        """确认中的批次必须只有一套可用字段，且同集市不能并行两套确认字段。"""
        inspected = inspect_columns(entry.get("字段定义"))
        if inspected["empty"]:
            return False, "确认失败：字段定义不能为空"
        if inspected["duplicates"]:
            return False, f"确认失败：字段定义重复（{'、'.join(inspected['duplicates'])}），只能保留一套字段"
        if _snapshot_mismatch(entry):
            return False, "确认失败：数据快照与字段定义不是同一版，不能带着混用快照进入确认"

        mart = find_mart(entry.get("集市编号"))
        if mart is not None:
            for sibling in batches_for_mart(mart):
                if int(sibling.get("id", 0)) == int(entry.get("id", 0)):
                    continue
                if batch_state(sibling) != "确认中":
                    continue
                if column_key(sibling.get("字段定义")) != column_key(entry.get("字段定义")):
                    return False, (
                        f"确认失败：批次{sibling.get('批次编号')}正在确认另一套字段，"
                        "同一集市同一时间只能有一套确认字段"
                    )

        set_batch_state(entry, "确认中")
        entry["字段定义"] = ",".join(inspected["columns"])
        entry["_snapshot_columns"] = inspected["columns"]
        entry["数据快照"] = _snapshot_label(str(entry.get("批次编号", "")), inspected["columns"])
        entry["字段指纹"] = _fingerprint(inspected["columns"])
        return True, ""

    def _publish(self, entry: dict[str, Any]) -> tuple[bool, str]:
        """原子发布：先完成全部校验，再一次性切换生效批与契约字段。"""
        inspected = inspect_columns(entry.get("字段定义"))
        if not inspected["single_field_set"]:
            return False, "发布失败：字段定义必须是非空且不重复的一套字段"
        if _snapshot_mismatch(entry):
            return False, "发布失败：数据快照与字段定义不一致，拒绝生成混用两批的快照"

        mart = find_mart(entry.get("集市编号"))
        if mart is None:
            return False, "发布失败：关联的结果集市不存在"

        mixed_with = _mixed_with_other_batches(entry, mart)
        if mixed_with:
            return False, f"发布失败：字段定义同时混入批次{mixed_with}的字段，只能发布一套字段"

        current = current_effective_batch(mart)
        contract = resolve_contract(entry.get("契约编号"), mart, current, entry)
        if contract is None:
            return False, "发布失败：未找到可承接的契约，无法校验字段一致性"
        if str(contract.get("status")) == "已终止":
            return False, "发布失败：关联契约已终止，不能承接新发布"

        contract_columns = normalize_columns(contract.get("Schema约束"))
        current_columns = normalize_columns(current.get("字段定义")) if current else []
        batch_columns = inspected["columns"]
        if contract_columns and column_key(contract_columns) not in {
            column_key(current_columns), column_key(batch_columns),
        }:
            return False, (
                "发布失败：契约当前字段与生效批、待发布批均不一致，"
                "疑似另一版字段定义正在推广，需先处理契约冲突"
            )

        promotion_columns = normalize_columns(contract.get("待推广字段"))
        if promotion_columns and column_key(promotion_columns) != column_key(batch_columns):
            return False, (
                "发布失败：契约侧刚校验通过的字段版本"
                f"[{','.join(promotion_columns)}]与本批字段"
                f"[{','.join(batch_columns)}]冲突，两版定义不能同时推广"
            )

        # 校验全部通过后再落状态，任何失败都不会动到原生效批。
        for sibling in batches_for_mart(mart):
            sibling["是否当前生效"] = False
            sibling["可承接查询"] = False
            if batch_state(sibling) == "已发布" and int(sibling.get("id", 0)) != int(entry.get("id", 0)):
                set_batch_state(sibling, "已下架")

        set_batch_state(entry, "已发布")
        entry["是否当前生效"] = True
        entry["可承接查询"] = True
        entry["发布时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["数据快照"] = _snapshot_label(str(entry.get("批次编号", "")), batch_columns)
        entry["_snapshot_columns"] = batch_columns
        entry["字段指纹"] = _fingerprint(batch_columns)
        entry["_contract_id"] = contract.get("id")
        entry["契约编号"] = contract.get("契约编号", "")
        contract["待推广字段"] = ""
        contract["挂起字段版本"] = ""

        # 契约字段跟随发布批；历史明细保留变更前后定义。
        if column_key(contract.get("Schema约束")) != column_key(batch_columns):
            before = str(contract.get("Schema约束", ""))
            contract["Schema约束"] = ",".join(batch_columns)
            contract["pending"] = False
            receipt = _publish_receipt(entry)
            write_contract_history(
                contract,
                change_type="批次发布生效",
                before_schema=before,
                after_schema=",".join(batch_columns),
                batch_no=str(entry.get("批次编号", "")),
                receipt_no=receipt,
                effective=True,
                note=f"批次{entry.get('批次编号')}发布生效，字段切换到新版本",
            )
        return True, ""

    def _rollback(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        from app.services.mart_release_rollback import MartReleaseRollbackService

        result = MartReleaseRollbackService().rollback_to_previous(
            mart_ref=entry.get("集市编号"),
            batch_ref=entry.get("批次编号"),
            reason="批次动作触发回滚到上一发布",
            operator="batch_operator",
        )
        if not result.get("ok"):
            return None, str(result.get("message", "回滚失败"))
        return entry, str(result.get("message", "回滚完成"))

    # ---------- 查询准入 ----------

    def admit_query(
        self,
        mart_ref: Any,
        contract_ref: Any = None,
        batch_ref: Any = None,
    ) -> dict[str, Any]:
        """判断当前生效批 + 契约组合能否承接新查询。"""
        mart = find_mart(mart_ref)
        if mart is None:
            return {"ok": False, "可承接查询": False, "issues": ["结果集市不存在"]}

        effective = current_effective_batch(mart)
        if effective is None:
            return {
                "ok": False, "可承接查询": False, "集市编号": mart.get("集市编号", ""),
                "当前生效批次": "", "当前生效契约": "", "issues": ["没有已发布的生效批次"],
            }

        if batch_ref:
            requested = next(
                (row for row in batches_for_mart(mart)
                 if str(row.get("批次编号", "")) == str(batch_ref)
                 or str(row.get("id", "")) == str(batch_ref)),
                None,
            )
            if requested is not None and int(requested.get("id", 0)) != int(effective.get("id", 0)):
                return {
                    "ok": False, "可承接查询": False, "集市编号": mart.get("集市编号", ""),
                    "当前生效批次": effective.get("批次编号", ""),
                    "当前生效契约": effective.get("契约编号", ""),
                    "issues": [f"指定批次{requested.get('批次编号')}不是当前生效批，不能承接新查询"],
                }

        contract = resolve_contract(contract_ref, mart, effective)
        issues: list[str] = []
        if contract is None:
            issues.append("未找到与生效批关联的契约")
        elif str(contract.get("status")) == "已终止":
            issues.append("契约已终止")
        elif column_key(contract.get("Schema约束")) != column_key(effective.get("字段定义")):
            issues.append(
                f"契约字段[{contract.get('Schema约束')}]与生效批字段"
                f"[{effective.get('字段定义')}]不一致"
            )

        if not effective.get("可承接查询"):
            issues.append("生效批未通过核验，已禁止承接新查询")
        if not effective.get("是否当前生效"):
            issues.append("批次未标记为当前生效")

        return {
            "ok": not issues,
            "可承接查询": not issues,
            "集市编号": mart.get("集市编号", ""),
            "当前生效批次": effective.get("批次编号", ""),
            "当前生效契约": contract.get("契约编号", "") if contract else "",
            "当前字段": effective.get("字段定义", ""),
            "契约字段": contract.get("Schema约束", "") if contract else "",
            "issues": issues,
        }


# ---------- 批次内部辅助 ----------

def _fingerprint(columns: list[str]) -> str:
    return hashlib.md5(",".join(columns).encode()).hexdigest()[:12]


def _snapshot_label(batch_no: str, columns: list[str]) -> str:
    return f"{batch_no}@{_fingerprint(columns)}"


def _snapshot_mismatch(entry: dict[str, Any]) -> bool:
    """快照必须来自本批次自己的一套字段，不能引用别的批次或历史拼装结果。"""
    snapshot = str(entry.get("数据快照", "")).strip()
    if not snapshot:
        return False
    batch_no = str(entry.get("批次编号", ""))
    if batch_no and not snapshot.startswith(batch_no):
        return True
    stored = entry.get("_snapshot_columns")
    if stored:
        return column_key(stored) != column_key(entry.get("字段定义"))
    return False


def _mixed_with_other_batches(entry: dict[str, Any], mart: dict[str, Any]) -> str:
    """识别“把两批字段拼成一批”的字段集合（并集或互相覆盖的拼装）。"""
    current = set(normalize_columns(entry.get("字段定义")))
    if not current:
        return ""
    others: list[tuple[str, set[str]]] = []
    for sibling in batches_for_mart(mart):
        if int(sibling.get("id", 0)) == int(entry.get("id", 0)):
            continue
        if not batch_belongs_to_mart(sibling, mart):
            continue
        columns = set(normalize_columns(sibling.get("字段定义")))
        if columns and columns != current:
            others.append((str(sibling.get("批次编号", "")), columns))

    # 情形一：两批字段互不相交，当前字段正好是两批的并集。
    for index, (left_no, left) in enumerate(others):
        for right_no, right in others[index + 1:]:
            if left & right:
                continue
            if left | right == current:
                return f"{left_no}+{right_no}"

    # 情形二：当前字段同时覆盖两批各自字段，且这两批互相不可比（不是正常追加字段）。
    covered = [(no, columns) for no, columns in others if columns <= current]
    for index, (left_no, left) in enumerate(covered):
        for right_no, right in covered[index + 1:]:
            if left <= right or right <= left:
                continue
            return f"{left_no}+{right_no}"
    return ""


def _publish_receipt(entry: dict[str, Any]) -> str:
    digest = hashlib.md5(
        f"{entry.get('批次编号')}::{entry.get('字段指纹')}".encode()
    ).hexdigest()[:8].upper()
    return f"RCPT-PUB-{digest}"


__all__ = ["MartReleaseBatchService", "previous_release_batch"]
