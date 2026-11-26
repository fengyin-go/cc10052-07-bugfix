"""分组回执与样本核对的专用 HTTP 接口。

通用 CRUD 仍在 ``resource.py``；这里把名单提交、断线领取、从回执发起核对和
剔除差异转核对等事务用可读路径暴露出来。所有路径都做了常见命名别名，便于既有
脚本按 batch/receipt/check/difference 等资源命名调用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.sample_ratio import SampleRatioService
from app.services.variant_assign import VariantAssignService

router = APIRouter(tags=["分组回执与样本差异"])
variant_service = VariantAssignService()
ratio_service = SampleRatioService()


async def _body(request: Request) -> dict[str, Any]:
    data: Any = {}
    if request.headers.get("content-type", "").lower().startswith("application/json"):
        try:
            data = await request.json()
        except Exception:
            data = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        data = {"items": data}
    # 查询参数作为默认值，JSON body 优先。
    return {**dict(request.query_params), **data}


# ---------- 名单提交 ----------

@router.post("/api/variant_assign/submit")
@router.post("/api/variant_assign/batches")
@router.post("/api/variant_assign/batch")
@router.post("/api/variant_assign/manifests")
@router.post("/api/variant_assign/batch_assign")
@router.post("/api/variant_assign/batch-assign")
@router.post("/api/variant_assign/submit-batch")
@router.post("/api/variant_assign/submit-list")
@router.post("/api/variant_assign/resend")
@router.post("/api/variant_assign/submitBatch")
@router.post("/api/variant_assign/batchAssign")
@router.post("/api/variant_assign/list-submit")
async def submit_manifest(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    headers = request.headers
    header_batch = (
        headers.get("idempotency-key")
        or headers.get("x-idempotency-key")
        or headers.get("x-batch-id")
        or headers.get("x-receipt-id")
    )
    if header_batch and not payload.get("batch_id"):
        payload["batch_id"] = header_batch
    exp_header = headers.get("x-experiment-code")
    if exp_header and not payload.get("experiment_code"):
        payload["experiment_code"] = exp_header
    return variant_service.submit_manifest(**payload)


# ---------- 回执查询 ----------

@router.get("/api/variant_assign/receipts")
@router.get("/api/variant_assign/receipt")
async def list_receipts(request: Request) -> dict[str, Any]:
    query = request.query_params
    identifier = (
        query.get("receipt_id") or query.get("batch_id") or query.get("id")
        or query.get("回执编号") or query.get("分配批次")
    )
    if identifier:
        receipt = variant_service.get_receipt(identifier)
        if receipt is None:
            return {"ok": False, "message": f"回执 {identifier} 不存在"}
        return receipt
    exp = query.get("实验编码") or query.get("exp_code") or query.get("experiment_code")
    items = variant_service.list_receipts(exp)
    return {"items": items, "total": len(items), "ok": True}


@router.get("/api/variant_assign/batches/{identifier}")
async def get_batch_as_receipt(identifier: str) -> dict[str, Any]:
    receipt = variant_service.get_receipt(identifier)
    if receipt is None:
        return {"ok": False, "message": f"回执 {identifier} 不存在"}
    return receipt


@router.get("/api/variant_assign/receipts/{identifier}")
@router.get("/api/variant_assign/receipt/{identifier}")
@router.get("/api/variant_assign/batches/{identifier}/receipt")
@router.get("/api/variant_assign/batch/{identifier}/receipt")
async def get_receipt(identifier: str) -> dict[str, Any]:
    receipt = variant_service.get_receipt(identifier)
    if receipt is None:
        return {"ok": False, "message": f"回执 {identifier} 不存在"}
    return receipt


# ---------- 继续领取 ----------

@router.post("/api/variant_assign/claim")
@router.post("/api/variant_assign/retry")
async def claim_receipt_from_body(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    identifier = (
        payload.pop("receipt_id", None) or payload.pop("batch_id", None)
        or payload.pop("id", None) or payload.pop("回执编号", None)
    )
    if identifier in (None, ""):
        return {"ok": False, "message": "缺少回执编号"}
    return variant_service.claim_receipt(str(identifier), **payload)


@router.post("/api/variant_assign/receipts/{identifier}/claim")
@router.post("/api/variant_assign/receipt/{identifier}/claim")
@router.post("/api/variant_assign/receipts/{identifier}/receive")
@router.post("/api/variant_assign/receipt/{identifier}/receive")
@router.post("/api/variant_assign/receipts/{identifier}/reclaim")
@router.post("/api/variant_assign/receipts/{identifier}/resume")
@router.post("/api/variant_assign/batches/{identifier}/claim")
@router.post("/api/variant_assign/batches/{identifier}/retry")
@router.post("/api/variant_assign/batches/{identifier}/resume")
@router.post("/api/variant_assign/receipts/{identifier}/claimPending")
async def claim_receipt(identifier: str, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    return variant_service.claim_receipt(identifier, **payload)


# ---------- 从回执发起实验核对 ----------

@router.post("/api/variant_assign/receipts/{identifier}/check")
@router.post("/api/variant_assign/receipt/{identifier}/check")
@router.post("/api/variant_assign/receipts/{identifier}/sample-check")
@router.post("/api/variant_assign/batches/{identifier}/check")
async def check_from_receipt(identifier: str, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    receipt = variant_service.get_receipt(identifier)
    if receipt is None:
        return {"ok": False, "message": f"回执 {identifier} 不存在"}
    exp = receipt.get("实验编码")
    return ratio_service.create_check(
        exp,
        source_receipt_id=identifier,
        **payload,
    )


# ---------- 单用户剔除的资源式别名 ----------

@router.post("/api/variant_assign/exclude")
async def exclude_assignment_from_body(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    entry_id = payload.get("assignment_id") or payload.get("entry_id") or payload.get("id")
    if entry_id in (None, ""):
        exp = (
        payload.get("experiment_code") or payload.get("experimentCode")
        or payload.get("exp_code") or payload.get("expCode")
        or payload.get("实验编码")
    )
        uid = payload.get("user_id") or payload.get("用户标识")
        assignment = VariantAssignService()._active_assignment(str(exp), str(uid)) if exp and uid else None
        if assignment is None:
            return {"ok": False, "message": "缺少 assignment_id，或找不到实验用户的有效分组"}
        entry_id = assignment.get("id")
    reason = str(payload.get("reason") or payload.get("remark") or payload.get("原因") or "接口剔除")
    return variant_service.exclude_user(int(entry_id), reason)


@router.post("/api/variant_assign/assignments/{entry_id}/exclude")
@router.post("/api/variant_assign/{entry_id}/exclude")
async def exclude_assignment(entry_id: int, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    reason = str(payload.get("reason") or payload.get("remark") or payload.get("原因") or "接口剔除")
    return variant_service.exclude_user(entry_id, reason)


# ---------- 样本核对计算入口 ----------

@router.get("/api/sample_ratio/snapshot")
@router.get("/api/sample_ratio/calculation-snapshot")
async def sample_snapshot(request: Request) -> dict[str, Any]:
    exp = request.query_params.get("experiment_code") or request.query_params.get("exp_code") \
        or request.query_params.get("实验编码")
    if not exp:
        return {"ok": False, "message": "缺少实验编码"}
    return VariantAssignService().experiment_snapshot(str(exp))


@router.post("/api/sample_ratio/checks")
@router.post("/api/sample_ratio/check")
@router.post("/api/sample_ratio/create-check")
@router.post("/api/sample_ratio/calculate")
@router.post("/api/sample_ratio/recompute")
@router.post("/api/sample_ratio/recalculate")
@router.post("/api/sample_ratio/calculateCheck")
@router.post("/api/sample_ratio/produce")
async def create_sample_check(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    exp = (
        payload.get("experiment_code") or payload.get("experimentCode")
        or payload.get("exp_code") or payload.get("expCode")
        or payload.get("实验编码")
    )
    path = request.url.path
    if any(token in path for token in ("/calculate", "/recompute", "/recalculate")):
        payload.setdefault("auto_run", True)
    return ratio_service.create_check(exp, **payload)


@router.post("/api/sample_ratio/run")
@router.post("/api/sample_ratio/run-check")
@router.post("/api/sample_ratio/execute")
async def run_sample_check_from_body(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    entry_id = payload.get("check_id") or payload.get("entry_id") or payload.get("id")
    if entry_id not in (None, "") and str(entry_id).isdigit():
        return ratio_service.run_check(int(entry_id))
    exp = (
        payload.get("experiment_code") or payload.get("experimentCode")
        or payload.get("exp_code") or payload.get("expCode")
        or payload.get("实验编码")
    )
    if not exp:
        return {"ok": False, "message": "缺少核对批次 ID 或实验编码"}
    payload.setdefault("auto_run", True)
    return ratio_service.create_check(exp, **payload)


@router.post("/api/sample_ratio/{entry_id}/run")
@router.post("/api/sample_ratio/{entry_id}/execute")
async def run_sample_check(entry_id: int) -> dict[str, Any]:
    return ratio_service.run_check(entry_id)


# ---------- 剔除差异 ----------

@router.get("/api/sample_ratio/difference")
@router.get("/api/sample_ratio/differences")
@router.get("/api/sample_ratio/diffs")
async def list_differences(request: Request) -> dict[str, Any]:
    exp = request.query_params.get("experiment_code") or request.query_params.get("exp_code") \
        or request.query_params.get("实验编码")
    identifier = request.query_params.get("id") or request.query_params.get("difference_id") \
        or request.query_params.get("差异编号")
    if identifier:
        diff = ratio_service.get_difference(identifier)
        if diff is None:
            return {"ok": False, "message": f"差异 {identifier} 不存在"}
        return {"ok": True, "difference": diff, "entry": diff}
    items = ratio_service.list_differences(exp)
    return {"items": items, "total": len(items), "ok": True}


@router.get("/api/sample_ratio/difference/{identifier}")
@router.get("/api/sample_ratio/differences/{identifier}")
@router.get("/api/sample_ratio/diffs/{identifier}")
async def get_difference(identifier: str) -> dict[str, Any]:
    diff = ratio_service.get_difference(identifier)
    if diff is None:
        return {"ok": False, "message": f"差异 {identifier} 不存在"}
    return {"ok": True, "difference": diff, "entry": diff}


@router.post("/api/sample_ratio/difference/{identifier}/check")
@router.post("/api/sample_ratio/differences/{identifier}/check")
@router.post("/api/sample_ratio/difference/{identifier}/new-check")
@router.post("/api/sample_ratio/differences/{identifier}/new-check")
@router.post("/api/sample_ratio/differences/{identifier}/initiate-check")
@router.post("/api/sample_ratio/diffs/{identifier}/check")
async def check_from_difference(identifier: str, request: Request) -> dict[str, Any]:
    payload = await _body(request)
    return ratio_service.create_check_from_difference(identifier, **payload)
