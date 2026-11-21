"""集市发布回滚领域服务：按发布批次回滚已生效的集市数据，并留下可追溯记录。

回滚状态严格按下架 → 校验 → 恢复推进：
- 下架：先把当前生效批摘除查询准入，避免回滚中途仍有新查询读到旧版；
- 校验：核对恢复批字段、契约当前字段与正在推广的字段版本，冲突则停在“校验”；
- 恢复：只有核验通过（或明确确认冲突）才切换生效批、回写契约与契约历史。

已领取文件在领取时冻结“确认版本”，回滚不会改写领取记录；对账时按冻结版本
读取，新查询则按恢复后的生效批读取，两种口径分别可查。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.mart_release_common import (
    batch_state,
    claim_batch,
    claim_columns,
    column_key,
    current_effective_batch,
    find_batch,
    find_claim,
    find_contract,
    find_mart,
    normalize_columns,
    previous_release_batch,
    resolve_contract,
    set_batch_state,
    write_contract_history,
    write_rollback_audit,
)
from app.store import store


class MartReleaseRollbackService(BaseModuleService):
    MODULE = "mart_release_rollback"
    ENTITY = "集市回滚记录"
    OPERATION_TERM = "回滚"
    LIST_FIELDS = [
        "回滚编号", "集市编号", "原发布批次", "恢复批次", "回滚前字段",
        "恢复后字段", "契约编号", "变更回执", "回滚状态", "回滚时间",
    ]
    REQUIRED_FIELDS = ["回滚编号", "集市编号"]
    STATUS_ORDER = ["下架", "校验", "恢复", "校验失败", "已终止"]
    ACTION_RULES = {"重新校验": "校验", "确认恢复": "恢复", "终止回滚": "已终止"}
    NEGATIVE_ACTIONS = ["终止回滚"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """回滚记录必须由发布批次触发，禁止手工登记后直接改状态。"""
        return None, ["回滚记录(由集市发布批次回滚生成，不支持手工登记)"]

    # ---------- 发起回滚 ----------

    def rollback_to_previous(
        self,
        *,
        mart_ref: Any,
        batch_ref: Any = None,
        reason: str = "",
        operator: str = "data_steward",
        accept_conflict: bool = False,
    ) -> dict[str, Any]:
        mart = find_mart(mart_ref)
        if mart is None:
            return {"ok": False, "message": f"结果集市{mart_ref}不存在"}

        active = _active_rollback(mart)
        if active is not None:
            return {
                "ok": False,
                "message": f"集市已有进行中的回滚 {active.get('回滚编号')}（{active.get('回滚状态')}），不能重复发起",
                "rollback": active,
            }

        current = find_batch(batch_ref) if batch_ref else current_effective_batch(mart)
        if current is None:
            return {"ok": False, "message": "集市没有可回滚的当前发布批"}
        if not _belongs(current, mart):
            return {"ok": False, "message": f"批次{current.get('批次编号')}不属于集市{mart.get('集市编号')}"}
        if batch_state(current) != "已发布":
            return {
                "ok": False,
                "message": f"批次{current.get('批次编号')}当前状态为「{batch_state(current)}」，只有已发布批次可以回滚",
            }

        previous = previous_release_batch(mart, current)
        if previous is None:
            return {"ok": False, "message": f"批次{current.get('批次编号')}之前没有可恢复的发布批"}

        contract = resolve_contract(current.get("契约编号"), mart, previous, current)
        if contract is None:
            return {"ok": False, "message": "未找到原发布批关联的契约，无法回滚契约字段"}

        receipt = _receipt_no(current, previous)
        rollback_id = _next_id(self.MODULE)
        record: dict[str, Any] = {
            "id": rollback_id,
            "回滚编号": f"MRBK-{rollback_id:04d}",
            "集市编号": mart.get("集市编号", ""),
            "原发布批次": current.get("批次编号", ""),
            "恢复批次": previous.get("批次编号", ""),
            "回滚前字段": current.get("字段定义", ""),
            "恢复后字段": previous.get("字段定义", ""),
            "契约编号": contract.get("契约编号", ""),
            "变更回执": receipt,
            "回滚状态": "下架",
            "回滚时间": "",
            "回滚原因": reason or "恢复上一次发布",
            "操作人": operator,
            "校验结果": "",
            "字段冲突": "",
            "可承接查询": False,
            "status": "下架",
            "pending": True,
            "abnormal": False,
            "_mart_id": mart.get("id"),
            "_contract_id": contract.get("id"),
            "_current_batch_id": current.get("id"),
            "_restore_batch_id": previous.get("id"),
            "_original_columns": normalize_columns(current.get("字段定义")),
            "_restore_columns": normalize_columns(previous.get("字段定义")),
            "_receipt": receipt,
        }
        store.rows(self.MODULE).append(record)

        # 第一步：下架。先把当前批摘除准入，再进入校验；失败可原地终止。
        _take_offline(current, mart)
        write_rollback_audit(record, "下架", f"当前批{current.get('批次编号')}已停止承接新查询")

        result = self._verify_and_restore(record, accept_conflict=accept_conflict)
        result["rollback"] = record
        return result

    # ---------- 通用动作入口 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        action = action.strip()
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        state = str(entry.get("回滚状态", ""))
        if action in {"重新校验", "确认恢复"}:
            if state not in {"下架", "校验", "校验失败"}:
                return None, f"回滚当前状态为「{state}」，不能执行「{action}」"
            result = self._verify_and_restore(entry, accept_conflict=action == "确认恢复")
            if not result.get("ok") and result.get("needs_confirmation"):
                return entry, f"{result.get('message')}；如确认以恢复批为准，请执行「确认恢复」"
            if not result.get("ok"):
                return None, str(result.get("message", "回滚校验失败"))
            return entry, str(result.get("message", "回滚完成"))

        # 终止回滚：把原发布批恢复为生效状态，撤回这次下架。
        if state not in {"下架", "校验", "校验失败"}:
            return None, f"回滚当前状态为「{state}」，不能执行「终止回滚」"
        _set_rollback_state(entry, "已终止")
        entry["可承接查询"] = False
        entry["回滚时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        original = find_batch(entry.get("_current_batch_id"))
        mart = find_mart(entry.get("集市编号"))
        if original is not None and mart is not None and batch_state(original) == "已下架":
            set_batch_state(original, "已发布")
            original["是否当前生效"] = True
            original["可承接查询"] = True
            _write_mart_projection(mart, original, find_contract(entry.get("_contract_id")), False)
        write_rollback_audit(entry, "终止回滚", "保留原发布批为生效批，未改写契约")
        return entry, "回滚已终止，原发布批恢复生效"

    # ---------- 校验与恢复 ----------

    def _verify_and_restore(
        self, record: dict[str, Any], *, accept_conflict: bool
    ) -> dict[str, Any]:
        mart = find_mart(record.get("集市编号"))
        restore = find_batch(record.get("_restore_batch_id"))
        current = find_batch(record.get("_current_batch_id"))
        contract = find_contract(record.get("_contract_id"))
        if mart is None or restore is None or contract is None:
            _set_rollback_state(record, "校验失败")
            record["校验结果"] = "关联集市、恢复批次或契约缺失"
            record["可承接查询"] = False
            return {"ok": False, "message": record["校验结果"]}

        _set_rollback_state(record, "校验")
        restore_columns = normalize_columns(restore.get("字段定义"))
        if not restore_columns:
            return self._fail(record, "恢复批次字段定义为空，无法恢复有效列约束")
        if not record.get("_restore_columns"):
            record["_restore_columns"] = restore_columns
        if column_key(record.get("_restore_columns")) != column_key(restore_columns):
            return self._fail(record, "恢复批次字段在回滚期间被改动，回滚快照与批次不一致")

        contract_columns = normalize_columns(contract.get("Schema约束"))
        conflicts: list[dict[str, Any]] = []
        if column_key(contract_columns) not in {
            column_key(restore_columns),
            column_key(current.get("字段定义")) if current else (),
        }:
            conflicts.append({
                "类型": "契约字段版本冲突",
                "契约当前字段": ",".join(contract_columns),
                "恢复批字段": ",".join(restore_columns),
                "说明": "契约已校验通过的字段版本与恢复批、原发布批都不同，疑似新版本正在推广",
            })
        # 同一契约上还有“刚通过校验待推广”的标记时，再确认一次版本是否收敛。
        if str(contract.get("status")) == "已终止":
            return self._fail(record, "关联契约已终止，恢复批不能作为生效定义")
        promotion_columns = normalize_columns(contract.get("待推广字段"))
        promotion_ready = bool(promotion_columns) and column_key(promotion_columns) != column_key(restore_columns)
        if not promotion_ready and not promotion_columns:
            promotion_ready = bool(contract.get("pending")) and column_key(contract_columns) != column_key(restore_columns)
        if promotion_ready:
            conflicts.append({
                "类型": "待推广契约版本冲突",
                "契约当前字段": ",".join(contract_columns),
                "待推广字段": ",".join(promotion_columns),
                "恢复批字段": ",".join(restore_columns),
                "说明": "契约侧有刚校验通过、准备推广的字段版本，与回滚目标版本不一致",
            })

        record["校验结果"] = "通过" if not conflicts else "字段版本冲突"
        record["字段冲突"] = "；".join(item["说明"] for item in conflicts)
        record["_conflicts"] = conflicts

        if conflicts and not accept_conflict:
            record["回滚状态"] = "校验"
            record["status"] = "校验"
            record["pending"] = True
            record["abnormal"] = True
            record["可承接查询"] = False
            _write_mart_projection(mart, None, contract, False)
            write_rollback_audit(
                record, "校验未通过", record["字段冲突"], abnormal=True
            )
            return {
                "ok": False,
                "needs_confirmation": True,
                "message": (
                    f"回滚校验发现字段版本冲突：{record['字段冲突']}。"
                    "当前集市不承接新查询，确认以恢复批为准后可执行「确认恢复」"
                ),
                "conflicts": conflicts,
                "current_effective": self.effective_view(mart),
            }

        # 恢复：契约回写到恢复批实际生效的定义，并写契约历史明细。
        before_schema = str(contract.get("Schema约束", ""))
        contract["Schema约束"] = ",".join(restore_columns)
        contract["pending"] = False
        contract["abnormal"] = False
        if str(contract.get("status")) in {"已违约", "草稿"}:
            contract["status"] = "已承诺"
        contract["挂起字段版本"] = ""
        contract["待推广字段"] = ""
        record["契约字段"] = ",".join(restore_columns)

        if current is not None:
            set_batch_state(current, "已下架")
            current["是否当前生效"] = False
            current["可承接查询"] = False
        set_batch_state(restore, "已发布")
        restore["是否当前生效"] = True
        restore["可承接查询"] = True
        restore["发布时间"] = time.strftime("%Y-%m-%d %H:%M:%S")

        _set_rollback_state(record, "恢复")
        record["可承接查询"] = True
        record["回滚时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record["校验结果"] = "通过（已确认冲突）" if conflicts else "通过"

        write_contract_history(
            contract,
            change_type="集市回滚恢复",
            before_schema=before_schema,
            after_schema=",".join(restore_columns),
            batch_no=str(restore.get("批次编号", "")),
            rollback_no=str(record.get("回滚编号", "")),
            receipt_no=str(record.get("变更回执", "")),
            effective=True,
            note=(
                f"回滚记录{record.get('回滚编号')}把契约字段从原发布批"
                f"{record.get('原发布批次')}恢复到{restore.get('批次编号')}"
            ),
            rollback_id=int(record.get("id", 0)),
        )
        _write_mart_projection(mart, restore, contract, True)
        _invalidate_batch_caches(record.get("原发布批次", ""))
        write_rollback_audit(
            record,
            "恢复",
            f"集市{mart.get('集市编号')}生效批={restore.get('批次编号')}，契约={contract.get('契约编号')}",
        )

        return {
            "ok": True,
            "message": (
                f"已回滚到批次{restore.get('批次编号')}，"
                f"当前生效契约{contract.get('契约编号')}，变更回执{record.get('变更回执')}"
            ),
            "conflicts": conflicts,
            "current_effective": self.effective_view(mart),
            "contract_history": next(
                (
                    row for row in store.rows("data_contract_history")
                    if int(row.get("_rollback_id") or 0) == int(record.get("id", 0))
                ),
                None,
            ),
        }

    def _fail(self, record: dict[str, Any], message: str) -> dict[str, Any]:
        _set_rollback_state(record, "校验失败")
        record["校验结果"] = message
        record["可承接查询"] = False
        record["回滚时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_rollback_audit(record, "校验失败", message, abnormal=True)
        mart = find_mart(record.get("集市编号"))
        if mart is not None:
            _write_mart_projection(mart, None, find_contract(record.get("_contract_id")), False)
        return {
            "ok": False,
            "message": f"回滚未完成：{message}",
            "rollback": record,
            "current_effective": self.effective_view(mart) if mart else None,
        }

    # ---------- 生效视图 ----------

    def effective_view(self, mart_ref: Any) -> dict[str, Any]:
        """交代当前生效的集市批与契约，以及该组合是否可承接查询。"""
        from app.services.mart_release_batch import MartReleaseBatchService

        mart = mart_ref if isinstance(mart_ref, dict) else find_mart(mart_ref)
        if mart is None:
            return {"ok": False, "message": f"结果集市{mart_ref}不存在"}
        batch_svc = MartReleaseBatchService()
        admission = batch_svc.admit_query(mart.get("集市编号"))
        effective = current_effective_batch(mart)
        contract = resolve_contract(effective.get("契约编号") if effective else None, mart, effective)
        latest = _latest_rollback(mart)
        return {
            "ok": True,
            "集市编号": mart.get("集市编号", ""),
            "集市状态": mart.get("status", ""),
            "当前生效批次": effective.get("批次编号", "") if effective else "",
            "当前生效契约": contract.get("契约编号", "") if contract else "",
            "当前字段": effective.get("字段定义", "") if effective else "",
            "契约字段": contract.get("Schema约束", "") if contract else "",
            "可承接查询": admission["可承接查询"],
            "核验问题": admission["issues"],
            "最近回滚": {
                "回滚编号": latest.get("回滚编号", ""),
                "回滚状态": latest.get("回滚状态", ""),
                "变更回执": latest.get("变更回执", ""),
            } if latest else None,
        }

    # ---------- 变更回执 ----------

    def trace_receipt(self, receipt_no: str) -> dict[str, Any]:
        """变更回执串起原始发布批次、回滚批次与契约历史，供审计追溯。"""
        receipt = str(receipt_no or "").strip()
        if not receipt:
            return {"ok": False, "message": "变更回执不能为空"}

        rollbacks = [
            row for row in store.rows(self.MODULE)
            if str(row.get("变更回执", "")) == receipt
        ]
        histories = [
            row for row in store.rows("data_contract_history")
            if str(row.get("变更回执", "")) == receipt
        ]
        publish_batches = [
            row for row in store.rows("mart_release_batch")
            if any(str(hist.get("关联批次", "")) == str(row.get("批次编号", "")) for hist in histories)
        ]
        if not rollbacks and not histories:
            return {"ok": False, "message": f"变更回执{receipt}没有对应的发布或回滚记录"}

        latest = rollbacks[-1] if rollbacks else None
        batches = {
            str(row.get("批次编号")): row
            for row in store.rows("mart_release_batch")
        }
        original_no = str(latest.get("原发布批次", "")) if latest else ""
        restore_no = str(latest.get("恢复批次", "")) if latest else ""
        # 回执可能只由一次发布产生（尚无回滚），此时原始发布批取契约历史里的关联批次。
        publish_no = str(publish_batches[-1].get("批次编号", "")) if publish_batches else ""
        original_no = original_no or publish_no
        return {
            "ok": True,
            "变更回执": receipt,
            "原始发布批次": original_no,
            "回滚批次": restore_no,
            "原始发布": _batch_brief(batches.get(original_no)),
            "回滚批次快照": _batch_brief(batches.get(restore_no)) if restore_no else None,
            "批次链路": [
                brief for brief in (
                    _batch_brief(batches.get(original_no)),
                    _batch_brief(batches.get(restore_no)) if restore_no else None,
                ) if brief
            ],
            "回滚记录": [_rollback_brief(row) for row in rollbacks],
            "契约历史": histories,
            "是否闭环": bool(rollbacks) and any(
                row.get("回滚状态") == "恢复" for row in rollbacks
            ),
        }

    # ---------- 领取对账 ----------

    def reconcile_claim(self, claim_ref: Any) -> dict[str, Any]:
        """按领取时冻结的确认版本对账，和新查询看到的定义分开呈现。"""
        claim = find_claim(claim_ref)
        if claim is None:
            return {"ok": False, "message": f"领取记录{claim_ref}不存在"}

        mart = find_mart(claim.get("集市编号"))
        claim_batch_row = claim_batch(claim)
        frozen = claim_columns(claim)
        effective = current_effective_batch(mart) if mart else None
        contract = resolve_contract(
            claim.get("契约编号"), mart, effective, claim_batch_row
        ) if mart else None
        new_columns = normalize_columns(effective.get("字段定义")) if effective else []
        batch_now = normalize_columns(claim_batch_row.get("字段定义")) if claim_batch_row else []

        renamed = [
            {"领取字段": column, "新查询字段": new_columns[index]}
            for index, column in enumerate(frozen)
            if index < len(new_columns) and column != new_columns[index]
        ]
        return {
            "ok": True,
            "领取编号": claim.get("领取编号", ""),
            "文件名称": claim.get("文件名称", ""),
            "领取批次": claim.get("批次编号", ""),
            "确认版本": ",".join(frozen),
            "领取时批次字段": ",".join(batch_now),
            "领取快照一致": column_key(frozen) == column_key(batch_now),
            "历史领取可读定义": ",".join(frozen),
            "新查询可读定义": ",".join(new_columns),
            "当前生效批次": effective.get("批次编号", "") if effective else "",
            "当前生效契约": contract.get("契约编号", "") if contract else "",
            "契约字段": contract.get("Schema约束", "") if contract else "",
            "字段差异": renamed,
            "对账口径": "历史领取按领取时确认版本读取；新查询按当前生效批读取",
        }


# ---------- 内部辅助 ----------

def _belongs(batch: dict[str, Any], mart: dict[str, Any]) -> bool:
    return str(batch.get("集市编号", "")) == str(mart.get("集市编号", "")) or str(
        batch.get("_mart_id")
    ) == str(mart.get("id"))


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _latest_rollback(mart: dict[str, Any]) -> dict[str, Any] | None:
    rows = _rollbacks_for_mart(mart)
    return max(rows, key=lambda row: int(row.get("id", 0))) if rows else None


def _active_rollback(mart: dict[str, Any]) -> dict[str, Any] | None:
    for row in _rollbacks_for_mart(mart):
        if row.get("回滚状态") in {"下架", "校验"}:
            return row
    return None


def _rollbacks_for_mart(mart: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(MartReleaseRollbackService.MODULE)
        if str(row.get("集市编号", "")) == str(mart.get("集市编号", ""))
        or str(row.get("_mart_id")) == str(mart.get("id"))
    ]


def _batch_brief(batch: dict[str, Any] | None) -> dict[str, Any] | None:
    if not batch:
        return None
    return {
        "批次编号": batch.get("批次编号", ""),
        "发布版本": batch.get("发布版本", ""),
        "字段定义": batch.get("字段定义", ""),
        "发布状态": batch.get("发布状态", batch.get("status", "")),
        "发布时间": batch.get("发布时间", ""),
        "契约编号": batch.get("契约编号", ""),
    }


def _rollback_brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "回滚编号": row.get("回滚编号", ""),
        "回滚状态": row.get("回滚状态", ""),
        "原发布批次": row.get("原发布批次", ""),
        "恢复批次": row.get("恢复批次", ""),
        "校验结果": row.get("校验结果", ""),
        "字段冲突": row.get("字段冲突", ""),
        "变更回执": row.get("变更回执", ""),
        "可承接查询": row.get("可承接查询", False),
    }


def _receipt_no(current: dict[str, Any], restore: dict[str, Any]) -> str:
    digest = hashlib.md5(
        f"{current.get('批次编号')}->{restore.get('批次编号')}::{time.time_ns()}".encode()
    ).hexdigest()[:8].upper()
    return f"RCPT-RBK-{digest}"


def _set_rollback_state(record: dict[str, Any], state: str) -> None:
    record["回滚状态"] = state
    record["status"] = state
    record["pending"] = state in {"下架", "校验"}
    record["abnormal"] = state in {"校验失败"}


def _take_offline(batch: dict[str, Any], mart: dict[str, Any]) -> None:
    batch["是否当前生效"] = False
    batch["可承接查询"] = False
    batch["status"] = "已下架"
    batch["发布状态"] = "已下架"
    mart["status"] = "刷新中"
    mart["可承接查询"] = False


def _write_mart_projection(
    mart: dict[str, Any],
    batch: dict[str, Any] | None,
    contract: dict[str, Any] | None,
    admit: bool,
) -> None:
    """把当前生效批与契约定向写回集市，作为回滚后的可见结果。"""
    mart["当前批次"] = batch.get("批次编号", "") if batch else ""
    mart["契约编号"] = contract.get("契约编号", "") if contract else ""
    mart["当前字段"] = batch.get("字段定义", "") if batch else ""
    mart["可承接查询"] = admit
    if batch is not None:
        mart["行数统计"] = str(batch.get("数据行数") or batch.get("行数统计") or mart.get("行数统计", "0"))
        mart["最后刷新"] = time.strftime("%Y-%m-%d %H:%M:%S")
        mart["status"] = "可用"
        mart["pending"] = False
        mart["abnormal"] = False
    elif not admit:
        mart["status"] = "刷新失败"
        mart["pending"] = False
        mart["abnormal"] = True


def _invalidate_batch_caches(batch_no: str) -> None:
    if not batch_no:
        return
    for cache in store.rows("result_cache"):
        if str(cache.get("关联批次", "")) == str(batch_no) and cache.get("status") == "有效":
            cache["status"] = "已过期"
            cache["pending"] = False
