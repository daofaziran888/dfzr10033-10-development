"""集市发布批次回滚：确认字段唯一、原子切换、回滚校验与契约回写。"""
import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.mart_release_batch import MartReleaseBatchService
from app.services.mart_release_rollback import MartReleaseRollbackService
from app.store import store

client = TestClient(app)
MART = "RESU-0001"
CONTRACT = 1

_ORIGINAL = {
    module: copy.deepcopy(store.rows(module))
    for module in (
        "mart_release_batch", "mart_release_rollback", "mart_release_claim",
        "data_contract", "data_contract_history", "result_mart", "query_audit",
        "result_cache",
    )
}


@pytest.fixture(autouse=True)
def reset_store():
    """每个用例前恢复相关模块的种子态，避免用例之间互相污染。"""
    for module, rows in _ORIGINAL.items():
        store._tables[module] = copy.deepcopy(rows)
    yield


def _create_batch(no: str, fields: str) -> dict:
    resp = client.post("/api/mart_release_batch", json={
        "values": {"批次编号": no, "集市编号": MART, "字段定义": fields},
    })
    assert resp.status_code == 200 and resp.json()["ok"], resp.json()
    return resp.json()["entry"]


def _publish(batch_id: int) -> list[dict]:
    out = []
    for action in ["提交确认", "确认发布", "发布生效"]:
        resp = client.post(f"/api/mart_release_batch/{batch_id}/actions", json={"action": action})
        out.append(resp.json())
    return out


def test_confirm_rejects_second_field_set_in_same_mart():
    first = _create_batch("RESU-T001", "id,name,amount")
    assert client.post(f"/api/mart_release_batch/{first['id']}/actions",
                       json={"action": "提交确认"}).json()["ok"]

    other = _create_batch("RESU-T002", "id,region,amount")
    result = client.post(f"/api/mart_release_batch/{other['id']}/actions",
                         json={"action": "提交确认"}).json()
    assert not result["ok"]
    assert "只能有一套确认字段" in result["message"]


def test_confirm_rejects_duplicate_or_empty_fields():
    dup = client.post("/api/mart_release_batch", json={
        "values": {"批次编号": "RESU-T003", "集市编号": MART, "字段定义": "id,name,name"}}).json()
    assert not dup["ok"] and "重复" in dup["message"]

    empty = client.post("/api/mart_release_batch", json={
        "values": {"批次编号": "RESU-T004", "集市编号": MART, "字段定义": "  ,  "}}).json()
    assert not empty["ok"]


def test_publish_failure_keeps_previous_effective_batch():
    batch = _create_batch("RESU-T005", "id,alias,amount")
    client.post(f"/api/mart_release_batch/{batch['id']}/actions", json={"action": "提交确认"})
    client.post(f"/api/mart_release_batch/{batch['id']}/actions", json={"action": "确认发布"})

    contract = store.find("data_contract", CONTRACT)
    contract["待推广字段"] = "id,alias,region,amount"
    result = client.post(f"/api/mart_release_batch/{batch['id']}/actions",
                         json={"action": "发布生效"}).json()
    assert not result["ok"] and "契约" in result["message"]

    view = client.get(f"/api/mart-release/{MART}/effective").json()
    assert view["当前生效批次"] == "RESU-B0002"
    assert view["可承接查询"] is True


def test_publish_rejects_mixed_snapshot_of_two_batches():
    batch = _create_batch("RESU-T006", "id,name,customer_name,amount")
    for action in ["提交确认", "确认发布"]:
        client.post(f"/api/mart_release_batch/{batch['id']}/actions", json={"action": action})
    result = client.post(f"/api/mart_release_batch/{batch['id']}/actions",
                         json={"action": "发布生效"}).json()
    assert not result["ok"] and "混入" in result["message"]
    assert client.get(f"/api/mart-release/{MART}/effective").json()["当前生效批次"] == "RESU-B0002"


def test_rollback_stops_at_verify_on_promotion_conflict_then_restores():
    contract = store.find("data_contract", CONTRACT)
    contract["待推广字段"] = "id,customer_name,amount,region"

    first = client.post(f"/api/mart-release/{MART}/rollback").json()
    assert first["ok"] is False and first["needs_confirmation"] is True
    record = first["rollback"]
    assert record["回滚状态"] == "校验"
    assert record["变更回执"].startswith("RCPT-RBK-")

    blocked = client.get(f"/api/mart-release/{MART}/admission").json()
    assert blocked["可承接查询"] is False
    view = client.get(f"/api/mart-release/{MART}/effective").json()
    assert view["当前生效批次"] == ""

    restored = client.post(f"/api/mart-release/rollbacks/{record['id']}/restore").json()
    assert restored["ok"] is True
    view = client.get(f"/api/mart-release/{MART}/effective").json()
    assert view["当前生效批次"] == "RESU-B0001"
    assert view["当前字段"] == "id,name,amount"
    assert view["契约字段"] == "id,name,amount"
    assert view["可承接查询"] is True


def test_rollback_recomputes_contract_constraints_and_history():
    rollback = client.post(f"/api/mart-release/{MART}/rollback").json()
    assert rollback["ok"] is True
    record = rollback["rollback"]

    rebuilt = client.post(f"/api/mart-release/contracts/{CONTRACT}/rebuild-constraints").json()
    assert rebuilt["ok"] and rebuilt["applied"] is True
    assert rebuilt["schema"] == "id,name,amount"
    assert rebuilt["restored_batch"] == "RESU-B0001"

    history = client.get(f"/api/mart-release/contracts/{CONTRACT}/history").json()["items"]
    rollback_rows = [row for row in history if row["关联回滚"] == record["回滚编号"]]
    assert any(row["变更类型"] == "集市回滚恢复" for row in rollback_rows)
    assert any(row["变更类型"] == "契约接口重算有效列约束" for row in rollback_rows)
    for row in rollback_rows:
        assert row["变更回执"] == record["变更回执"]
        assert row["生效状态"] == "已生效"

    contract = store.find("data_contract", CONTRACT)
    assert contract["Schema约束"] == "id,name,amount"


def test_claim_reconciliation_keeps_original_version_after_rename_and_rollback():
    # 先回到改名前的发布批；历史领取仍按确认版本读取，新查询读恢复批。
    client.post(f"/api/mart-release/{MART}/rollback")

    old_claim = client.get("/api/mart-release/claims/CLAIM-0001/reconcile").json()
    assert old_claim["历史领取可读定义"] == "id,name,amount"
    assert old_claim["新查询可读定义"] == "id,name,amount"
    assert old_claim["领取快照一致"] is True

    renamed_claim = client.get("/api/mart-release/claims/CLAIM-0002/reconcile").json()
    assert renamed_claim["历史领取可读定义"] == "id,customer_name,amount"
    assert renamed_claim["新查询可读定义"] == "id,name,amount"
    assert {"领取字段": "customer_name", "新查询字段": "name"} in renamed_claim["字段差异"]
    assert renamed_claim["当前生效批次"] == "RESU-B0001"


def test_receipt_trace_links_original_and_rollback_batches():
    rollback = client.post(f"/api/mart-release/{MART}/rollback").json()["rollback"]
    trace = client.get(f"/api/mart-release/receipts/{rollback['变更回执']}").json()
    assert trace["ok"] is True
    assert trace["原始发布批次"] == "RESU-B0002"
    assert trace["回滚批次"] == "RESU-B0001"
    assert trace["是否闭环"] is True
    assert trace["批次链路"][0]["批次编号"] == "RESU-B0002"
    assert trace["批次链路"][1]["批次编号"] == "RESU-B0001"


def test_admission_after_rollback_and_contract_mismatch():
    svc = MartReleaseBatchService()
    assert svc.admit_query(MART)["可承接查询"] is True

    client.post(f"/api/mart-release/{MART}/rollback")
    contract = store.find("data_contract", CONTRACT)
    contract["Schema约束"] = "id,other,amount"  # 模拟契约被另一版覆盖
    blocked = client.get(f"/api/mart-release/{MART}/admission").json()
    assert blocked["可承接查询"] is False
    assert any("不一致" in issue for issue in blocked["issues"])


def test_rollback_requires_previous_release():
    # 回到只有一批的集市：没有上一发布批时应拒绝。
    store._tables["mart_release_batch"] = [
        row for row in store.rows("mart_release_batch") if row.get("批次编号") != "RESU-B0001"
    ]
    result = MartReleaseRollbackService().rollback_to_previous(mart_ref=MART)
    assert result["ok"] is False
    assert "没有可恢复的发布批" in result["message"]
