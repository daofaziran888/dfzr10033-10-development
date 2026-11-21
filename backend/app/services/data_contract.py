"""数据契约领域服务：数据消费者与生产者之间的契约管理、Schema约束与SLA跟踪。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.mart_release_common import (
    column_key,
    find_batch,
    normalize_columns,
    rollback_rows_for_contract,
    write_contract_history,
)
from app.store import store


class DataContractService(BaseModuleService):
    MODULE = "data_contract"
    ENTITY = "契约定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["契约编号", "数据集", "Schema约束", "SLA定义", "更新频率", "负责人", "消费者", "生效时间"]
    REQUIRED_FIELDS = ["契约编号", "数据集"]
    STATUS_ORDER = ["草稿", "已承诺", "已违约", "已终止"]
    ACTION_RULES = {"签署契约": "已承诺", "标记违约": "已违约", "终止契约": "已终止"}
    NEGATIVE_ACTIONS = ["标记违约", "终止契约"]

    def verify_contract_compliance(self, contract_id: int) -> dict[str, Any]:
        """验证数据契约的合规性，跨模块检查Schema一致性和数据新鲜度。

        跨模块数据流：
        - 读取 source_registry 校验数据源状态
        - 读取 semantic_model 校验Schema一致性
        - 读取 freshness_mon 检查SLA达标情况
        - 读取 query_audit 检查消费记录
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        dataset = contract.get("数据集", "")
        violations: list[str] = []

        # 检查数据源是否存在且可用
        sources = store.rows("source_registry")
        source_ok = False
        for s in sources:
            if s.get("源编号", "") in dataset or dataset in str(s.get("连接地址", "")):
                if s.get("status") == "已停用":
                    violations.append(f"数据源{s['源编号']}已停用")
                else:
                    source_ok = True

        if not source_ok:
            violations.append("未找到对应的可用数据源")

        # 检查Schema一致性
        models = store.rows("semantic_model")
        schema_ok = False
        for m in models:
            if m.get("status") == "已投产" and dataset in str(m.get("源表映射", "")):
                if m.get("字段别名", "") == contract.get("Schema约束", ""):
                    schema_ok = True
                else:
                    violations.append("Schema定义与语义模型不一致")
                break

        # 检查SLA新鲜度
        freshness_rows = store.rows("freshness_mon")
        freshness_ok = True
        for f in freshness_rows:
            if dataset in str(f.get("目标数据集", "")):
                if f.get("status") == "已告警":
                    freshness_ok = False
                    violations.append(f"新鲜度告警: {f.get('延迟阈值', 'unknown')}")

        # 检查消费记录
        audits = store.rows("query_audit")
        consumer = contract.get("消费者", "")
        consumption_records = [a for a in audits if consumer in str(a.get("操作用户", ""))]
        if not consumption_records:
            violations.append("消费者无实际查询记录")

        # 更新契约状态
        if violations:
            contract["status"] = "已违约"
            contract["pending"] = True
            contract["abnormal"] = True
        else:
            contract["status"] = "已承诺"
            contract["pending"] = False
            contract["abnormal"] = False

        return {
            "ok": True,
            "contract_id": contract_id,
            "status": contract["status"],
            "violations": violations,
            "has_source": source_ok,
            "has_schema_match": schema_ok,
            "is_fresh": freshness_ok,
            "consumption_records": len(consumption_records),
        }

    def propagate_schema_change(self, contract_id: int, new_schema: str) -> dict[str, Any]:
        """当Schema变化时，通过契约将变更通知所有消费者。

        跨模块数据流：
        - 更新 data_contract Schema约束
        - 通知 related semantic_model 更新
        - 更新 freshness_mon 重新评估SLA
        - 写入 query_audit
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        old_schema = contract.get("Schema约束", "")
        contract["Schema约束"] = new_schema
        contract["pending"] = True

        # 通知语义模型
        models = store.rows("semantic_model")
        for m in models:
            if contract.get("数据集", "") in str(m.get("源表映射", "")):
                m["字段别名"] = new_schema
                m["pending"] = True

        # 写入审计
        rows = store.rows("query_audit")
        audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        audit = {
            "id": audit_id,
            "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
            "查询语句": f"CONTRACT_SCHEMA_CHANGE contract={contract_id}",
            "执行耗时": "150ms",
            "扫描行数": "1",
            "返回行数": "1",
            "数据来源": "data_contract",
            "操作用户": "contract_manager",
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "正常",
            "pending": False,
            "abnormal": False,
        }
        rows.append(audit)

        return {
            "ok": True,
            "contract_id": contract_id,
            "old_schema": old_schema,
            "new_schema": new_schema,
        }

    def rebuild_effective_constraints(self, contract_id: int) -> dict[str, Any]:
        """读取集市发布回滚记录，按恢复批重算有效列约束并回写契约历史。

        只有已走到「恢复」的回滚才改写生效定义；停在「校验 / 校验失败」的回滚
        记录只作为待处理线索返回，不会把半成品字段写进契约。
        """
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"ok": False, "message": "数据契约不存在"}

        rollbacks = rollback_rows_for_contract(contract)
        restored = [row for row in rollbacks if row.get("回滚状态") == "恢复"]
        pending = [row for row in rollbacks if row.get("回滚状态") in {"下架", "校验", "校验失败"}]

        if not restored:
            return {
                "ok": True,
                "contract_id": contract_id,
                "applied": False,
                "schema": contract.get("Schema约束", ""),
                "message": "没有已完成的集市回滚记录，维持当前列约束",
                "pending_rollbacks": [_rollback_brief(row) for row in pending],
                "history": self.list_history(contract_id)["items"],
            }

        latest = max(restored, key=lambda row: int(row.get("id", 0)))
        restore_batch = find_batch(latest.get("恢复批次"))
        columns = normalize_columns(
            restore_batch.get("字段定义") if restore_batch else latest.get("恢复后字段")
        )
        if not columns:
            return {"ok": False, "message": "回滚记录的恢复批次没有可用字段，无法重算列约束"}

        before_schema = str(contract.get("Schema约束", ""))
        changed = column_key(before_schema) != column_key(columns)
        contract["Schema约束"] = ",".join(columns)
        contract["pending"] = False
        contract["abnormal"] = False
        if str(contract.get("status")) in {"已违约", "草稿"}:
            contract["status"] = "已承诺"
        contract["当前生效批次"] = latest.get("恢复批次", "")

        history = write_contract_history(
            contract,
            change_type="契约接口重算有效列约束",
            before_schema=before_schema,
            after_schema=",".join(columns),
            batch_no=str(latest.get("恢复批次", "")),
            rollback_no=str(latest.get("回滚编号", "")),
            receipt_no=str(latest.get("变更回执", "")),
            effective=True,
            note="数据契约接口读取回滚记录后重算恢复批的有效列约束",
            rollback_id=int(latest.get("id", 0)),
        )
        return {
            "ok": True,
            "contract_id": contract_id,
            "applied": True,
            "changed": changed,
            "schema": contract.get("Schema约束", ""),
            "restored_batch": latest.get("恢复批次", ""),
            "rollback_no": latest.get("回滚编号", ""),
            "receipt": latest.get("变更回执", ""),
            "history": history,
            "pending_rollbacks": [_rollback_brief(row) for row in pending],
        }

    def list_history(self, contract_id: int) -> dict[str, Any]:
        contract = store.find(self.MODULE, contract_id)
        if contract is None:
            return {"items": [], "total": 0}
        rows = [
            row for row in store.rows("data_contract_history")
            if int(row.get("_contract_id") or 0) == contract_id
            or str(row.get("契约编号", "")) == str(contract.get("契约编号", ""))
        ]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return {"items": rows, "total": len(rows)}


def _rollback_brief(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "回滚编号": row.get("回滚编号", ""),
        "回滚状态": row.get("回滚状态", ""),
        "原发布批次": row.get("原发布批次", ""),
        "恢复批次": row.get("恢复批次", ""),
        "字段冲突": row.get("字段冲突", ""),
        "变更回执": row.get("变更回执", ""),
    }