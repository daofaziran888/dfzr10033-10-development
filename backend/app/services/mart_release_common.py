"""集市发布/回滚共享工具：批次解析、字段规整、领取快照与契约历史写入。

发布批次、回滚记录和契约三块逻辑都要用到同一套“哪些字段算一套可用定义”
的判断，放在这里避免各服务各自实现后口径漂移。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.store import store

BATCH_MODULE = "mart_release_batch"
ROLLBACK_MODULE = "mart_release_rollback"
CLAIM_MODULE = "mart_release_claim"
CONTRACT_HISTORY_MODULE = "data_contract_history"

# 只有这些状态的批次可以参与“上一次发布”的候选；草稿/确认中还没有形成发布版本。
RELEASED_STATES = {"待发布", "已发布", "已下架"}
EFFECTIVE_STATES = {"已发布"}


def normalize_columns(value: Any) -> list[str]:
    """把字段定义规整为去空白、去重且保持顺序的列名列表。"""
    if isinstance(value, (list, tuple, set)):
        raw_parts: Iterable[Any] = list(value)
    else:
        raw_parts = str(value or "").replace("，", ",").split(",")
    columns: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        column = str(part).strip()
        if column and column not in seen:
            seen.add(column)
            columns.append(column)
    return columns


def column_key(value: Any) -> tuple[str, ...]:
    """字段集合的比较键；顺序不影响“是否同一套字段”的判断。"""
    return tuple(sorted(normalize_columns(value)))


def inspect_columns(value: Any) -> dict[str, Any]:
    """检查字段定义是否为空、是否重复，供“确认中批次只有一套字段”校验。"""
    if isinstance(value, (list, tuple, set)):
        raw_parts = [str(part).strip() for part in value]
    else:
        raw_parts = [part.strip() for part in str(value or "").replace("，", ",").split(",")]
    raw = [part for part in raw_parts if part]
    columns = normalize_columns(value)
    duplicates = sorted({part for part in raw if raw.count(part) > 1})
    return {
        "raw": raw,
        "columns": columns,
        "duplicates": duplicates,
        "empty": not columns,
        "single_field_set": bool(columns) and not duplicates,
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def find_mart(mart_ref: Any) -> dict[str, Any] | None:
    """按 id 或集市编号定位结果集市。"""
    if mart_ref is None or str(mart_ref).strip() == "":
        return None
    ref = str(mart_ref).strip()
    for row in store.rows("result_mart"):
        if str(row.get("id")) == ref or str(row.get("集市编号", "")) == ref:
            return row
    return None


def find_batch(batch_ref: Any) -> dict[str, Any] | None:
    """按 id 或批次编号定位发布批次。"""
    if batch_ref is None or str(batch_ref).strip() == "":
        return None
    ref = str(batch_ref).strip()
    for row in store.rows(BATCH_MODULE):
        if str(row.get("id")) == ref or str(row.get("批次编号", "")) == ref:
            return row
    return None


def batch_state(batch: dict[str, Any]) -> str:
    return str(batch.get("发布状态") or batch.get("status") or "")


def set_batch_state(batch: dict[str, Any], state: str) -> None:
    """同时维护展示字段和通用 status，避免两处口径不一致。"""
    batch["发布状态"] = state
    batch["status"] = state
    batch["pending"] = state in {"草稿", "确认中", "待发布"}
    batch["abnormal"] = state in {"发布失败"}


def batch_belongs_to_mart(batch: dict[str, Any], mart: dict[str, Any]) -> bool:
    batch_mart = str(batch.get("集市编号", "")).strip()
    if batch_mart and mart.get("集市编号"):
        return batch_mart == str(mart.get("集市编号"))
    batch_mart_id = str(batch.get("集市ID") or batch.get("_mart_id") or "").strip()
    return bool(batch_mart_id) and batch_mart_id == str(mart.get("id"))


def batches_for_mart(mart: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in store.rows(BATCH_MODULE) if batch_belongs_to_mart(row, mart)]


def release_sequence(batch: dict[str, Any]) -> int:
    return _as_int(batch.get("发布序号") or batch.get("id"))


def current_effective_batch(mart: dict[str, Any]) -> dict[str, Any] | None:
    """当前生效批：同一集市里状态为“已发布”的最新发布序号。"""
    published = [
        batch for batch in batches_for_mart(mart)
        if batch_state(batch) in EFFECTIVE_STATES
    ]
    if not published:
        return None
    return max(published, key=lambda batch: (release_sequence(batch), _as_int(batch.get("id"))))


def previous_release_batch(
    mart: dict[str, Any], current: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """找到当前发布批之前最近的一个发布版本。"""
    current = current or current_effective_batch(mart)
    current_seq = release_sequence(current) if current else 10**12
    current_id = _as_int(current.get("id")) if current else -1
    candidates = [
        batch for batch in batches_for_mart(mart)
        if _as_int(batch.get("id")) != current_id
        and batch_state(batch) in RELEASED_STATES
        and release_sequence(batch) < current_seq
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda batch: (release_sequence(batch), _as_int(batch.get("id"))))


def find_contract(contract_ref: Any) -> dict[str, Any] | None:
    if contract_ref is None or str(contract_ref).strip() == "":
        return None
    ref = str(contract_ref).strip()
    for row in store.rows("data_contract"):
        if str(row.get("id")) == ref or str(row.get("契约编号", "")) == ref:
            return row
    return None


def contract_belongs_to_mart(contract: dict[str, Any], mart: dict[str, Any]) -> bool:
    dataset = str(contract.get("数据集", ""))
    if not dataset:
        return False
    return any(
        token and token in dataset
        for token in (str(mart.get("集市编号", "")), str(mart.get("集市名称", "")))
    )


def resolve_contract(
    contract_ref: Any,
    mart: dict[str, Any],
    *batches: dict[str, Any] | None,
) -> dict[str, Any] | None:
    contract = find_contract(contract_ref)
    if contract is not None:
        return contract
    for batch in batches:
        if not batch:
            continue
        contract = find_contract(batch.get("契约编号"))
        if contract is not None:
            return contract
    contract = find_contract(mart.get("契约编号"))
    if contract is not None:
        return contract
    # 最后按数据集兜底，只有唯一命中时才采用，避免回滚串到别的契约。
    matches = [
        row for row in store.rows("data_contract")
        if contract_belongs_to_mart(row, mart) and row.get("status") != "已终止"
    ]
    return matches[0] if len(matches) == 1 else None


def claims_for_mart(mart: dict[str, Any]) -> list[dict[str, Any]]:
    mart_no = str(mart.get("集市编号", ""))
    mart_id = str(mart.get("id"))
    claims = []
    for claim in store.rows(CLAIM_MODULE):
        claim_mart = str(claim.get("集市编号", ""))
        if claim_mart == mart_no:
            claims.append(claim)
        elif str(claim.get("_mart_id", "")) == mart_id:
            claims.append(claim)
    return claims


def find_claim(claim_ref: Any) -> dict[str, Any] | None:
    if claim_ref is None or str(claim_ref).strip() == "":
        return None
    ref = str(claim_ref).strip()
    for row in store.rows(CLAIM_MODULE):
        if str(row.get("id")) == ref or str(row.get("领取编号", "")) == ref:
            return row
    return None


def claim_columns(claim: dict[str, Any]) -> list[str]:
    """领取文件冻结的是确认时的字段版本，后续发布/回滚都不能改写它。"""
    return normalize_columns(
        claim.get("确认版本") or claim.get("确认字段") or claim.get("字段快照")
    )


def claim_batch(claim: dict[str, Any]) -> dict[str, Any] | None:
    batch = find_batch(claim.get("批次编号"))
    if batch is not None:
        return batch
    return find_batch(claim.get("_batch_id"))


def next_row_id(module: str) -> int:
    return max((_as_int(row.get("id")) for row in store.rows(module)), default=0) + 1


def next_code(module: str, field: str, prefix: str) -> str:
    candidate_id = next_row_id(module)
    existing = {str(row.get(field, "")) for row in store.rows(module)}
    code = f"{prefix}-{candidate_id:04d}"
    while code in existing:
        candidate_id += 1
        code = f"{prefix}-{candidate_id:04d}"
    return code


def write_contract_history(
    contract: dict[str, Any],
    *,
    change_type: str,
    before_schema: str,
    after_schema: str,
    batch_no: str = "",
    rollback_no: str = "",
    receipt_no: str = "",
    effective: bool = True,
    note: str = "",
    rollback_id: int = 0,
) -> dict[str, Any]:
    """写契约历史明细；同一回滚的同类回写更新原明细，保持可追溯。"""
    rows = store.rows(CONTRACT_HISTORY_MODULE)
    existing = None
    if rollback_id:
        existing = next(
            (
                row for row in rows
                if _as_int(row.get("_rollback_id")) == rollback_id
                and str(row.get("变更类型", "")) == change_type
            ),
            None,
        )
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if existing is not None:
        existing.update({
            "变更类型": change_type,
            "变更前Schema": before_schema,
            "变更后Schema": after_schema,
            "关联批次": batch_no,
            "关联回滚": rollback_no,
            "变更回执": receipt_no,
            "生效状态": "已生效" if effective else "未生效",
            "变更说明": note,
            "变更时间": now,
            "status": "已生效" if effective else "未生效",
            "pending": not effective,
            "abnormal": not effective,
        })
        return existing

    history_id = next_row_id(CONTRACT_HISTORY_MODULE)
    row = {
        "id": history_id,
        "历史编号": f"MCHS-{history_id:04d}",
        "契约编号": contract.get("契约编号", ""),
        "数据集": contract.get("数据集", ""),
        "变更类型": change_type,
        "变更前Schema": before_schema,
        "变更后Schema": after_schema,
        "关联批次": batch_no,
        "关联回滚": rollback_no,
        "变更回执": receipt_no,
        "生效状态": "已生效" if effective else "未生效",
        "变更说明": note,
        "变更时间": now,
        "status": "已生效" if effective else "未生效",
        "pending": not effective,
        "abnormal": not effective,
        "_contract_id": _as_int(contract.get("id")),
        "_rollback_id": rollback_id,
    }
    rows.append(row)
    return row


def write_rollback_audit(
    rollback: dict[str, Any], action: str, detail: str = "", abnormal: bool = False
) -> None:
    rows = store.rows("query_audit")
    audit_id = next_row_id("query_audit")
    rows.append({
        "id": audit_id,
        "记录编号": f"AUDIT-MRBK-{audit_id:04d}",
        "查询语句": (
            f"MART_ROLLBACK {rollback.get('回滚编号', '')} "
            f"from={rollback.get('原发布批次', '')} to={rollback.get('恢复批次', '')} "
            f"action={action}"
        ),
        "执行耗时": "80ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": ROLLBACK_MODULE,
        "操作用户": rollback.get("操作人", "data_steward"),
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "慢查询" if abnormal else "正常",
        "pending": False,
        "abnormal": abnormal,
        "备注": detail,
    })


def rollback_rows_for_contract(contract: dict[str, Any]) -> list[dict[str, Any]]:
    contract_id = _as_int(contract.get("id"))
    contract_no = str(contract.get("契约编号", ""))
    rows = [
        row for row in store.rows(ROLLBACK_MODULE)
        if _as_int(row.get("_contract_id")) == contract_id
        or (contract_no and str(row.get("契约编号", "")) == contract_no)
    ]
    return sorted(rows, key=lambda row: _as_int(row.get("id")))
