"""集市发布与回滚接口：批次发布、生效视图、查询准入、领取对账与契约回写。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.data_contract import DataContractService
from app.services.mart_release_batch import MartReleaseBatchService
from app.services.mart_release_claim import MartReleaseClaimService
from app.services.mart_release_rollback import MartReleaseRollbackService

router = APIRouter(prefix="/api/mart-release", tags=["集市发布回滚"])

batch_svc = MartReleaseBatchService()
rollback_svc = MartReleaseRollbackService()
claim_svc = MartReleaseClaimService()
contract_svc = DataContractService()


@router.get("/{mart_ref}/effective")
def effective_batch(mart_ref: str) -> dict[str, Any]:
    """交代当前生效的集市批与契约，以及该组合能否承接新查询。"""
    return rollback_svc.effective_view(mart_ref)


@router.get("/{mart_ref}/admission")
def query_admission(
    mart_ref: str, contract: str | None = Query(default=None),
    batch: str | None = Query(default=None),
) -> dict[str, Any]:
    """核验当前生效批 + 契约组合；未通过核验的组合不允许承接新查询。"""
    return batch_svc.admit_query(mart_ref, contract, batch)


@router.post("/{mart_ref}/rollback")
def rollback(
    mart_ref: str,
    batch: str | None = Query(default=None),
    reason: str = Query(default="恢复上一次发布"),
    operator: str = Query(default="data_steward"),
    accept_conflict: bool = Query(default=False),
) -> dict[str, Any]:
    """按发布批次回滚到上一次发布；冲突默认停在「校验」等待确认。"""
    result = rollback_svc.rollback_to_previous(
        mart_ref=mart_ref, batch_ref=batch, reason=reason,
        operator=operator, accept_conflict=accept_conflict,
    )
    if not result.get("ok") and not result.get("needs_confirmation"):
        raise HTTPException(status_code=409, detail=str(result.get("message", "回滚失败")))
    if isinstance(result.get("rollback"), dict):
        result["rollback"] = _public(result["rollback"])
    return result


@router.post("/rollbacks/{rollback_ref}/verify")
def verify_rollback(rollback_ref: str) -> dict[str, Any]:
    record = _find_rollback(rollback_ref)
    entry, message = rollback_svc.run_action(int(record["id"]), "重新校验")
    return {"ok": entry is not None, "message": message, "entry": _public(entry) if entry else None}


@router.post("/rollbacks/{rollback_ref}/restore")
def confirm_restore(rollback_ref: str) -> dict[str, Any]:
    record = _find_rollback(rollback_ref)
    entry, message = rollback_svc.run_action(int(record["id"]), "确认恢复")
    return {"ok": entry is not None, "message": message, "entry": _public(entry) if entry else None}


@router.get("/rollbacks/{rollback_ref}")
def rollback_detail(rollback_ref: str) -> dict[str, Any]:
    return _public(_find_rollback(rollback_ref))


@router.get("/claims/{claim_ref}/reconcile")
def reconcile_claim(claim_ref: str) -> dict[str, Any]:
    """分别核对历史领取与新查询能读到的字段定义。"""
    result = rollback_svc.reconcile_claim(claim_ref)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("message")))
    return result


@router.post("/contracts/{contract_id}/rebuild-constraints")
def rebuild_contract_constraints(contract_id: int) -> dict[str, Any]:
    """契约接口读取回滚记录，重算恢复批的有效列约束并回写契约历史。"""
    result = contract_svc.rebuild_effective_constraints(contract_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=str(result.get("message")))
    return result


@router.get("/contracts/{contract_id}/history")
def contract_history(contract_id: int) -> dict[str, Any]:
    return contract_svc.list_history(contract_id)


@router.get("/receipts/{receipt_no}")
def receipt_trace(receipt_no: str) -> dict[str, Any]:
    """变更回执串起原始发布批次与回滚批次。"""
    result = rollback_svc.trace_receipt(receipt_no)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("message")))
    return result


def _find_rollback(rollback_ref: str) -> dict[str, Any]:
    rows, _ = rollback_svc.list_entries(page=1, size=1_000_000)
    for row in rows:
        if str(row.get("id")) == str(rollback_ref) or str(row.get("回滚编号", "")) == str(rollback_ref):
            return row
    raise HTTPException(status_code=404, detail=f"回滚记录{rollback_ref}不存在")


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}
