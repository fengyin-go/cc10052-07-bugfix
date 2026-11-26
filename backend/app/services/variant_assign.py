"""分组分配器领域服务：名单提交、幂等回执、冲突领取与权威归属。

核心身份是 ``(实验编码, 用户标识)`` 的当前有效归属。名单内重复、跨批次重发和
并发领取都只能保留一个有效分组结果；回执保留每一条明细的确认、合并和未办状态，
供断线重连后继续领取。样本量核对只读取这里确认过的有效用户。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

RECEIPT_TABLE = "assignment_receipt"
LOCK_TABLE = "assignment_claim_lock"
DIFF_TABLE = "sample_ratio_difference"

PENDING = "未办"
CONFIRMED = "已确认"
MERGED = "已合并"
CONFLICT = "冲突"
DONE_STATES = {"confirmed", "merged"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def norm_user(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _pick(source: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        for key in keys:
            value = source.get(key)
            if value not in (None, "", []):
                return value
    return default


def _remark_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, dict):
        return str(
            payload.get("remark") or payload.get("原因") or payload.get("reason")
            or payload.get("剔除原因") or ""
        )
    return str(
        getattr(payload, "remark", "")
        or getattr(payload, "reason", "")
        or getattr(payload, "原因", "")
        or ""
    )


def _payload_value(payload: Any, *keys: str) -> Any:
    if payload is None:
        return None
    if isinstance(payload, dict):
        return _pick(payload, *keys)
    for key in keys:
        value = getattr(payload, key, None)
        if value not in (None, ""):
            return value
    return None


class VariantAssignService(BaseModuleService):
    MODULE = "variant_assign"
    ENTITY = "分组结果"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["分配批次", "用户标识", "实验编码", "分组名称", "分配时间"]
    REQUIRED_FIELDS = ["分配批次", "用户标识", "实验编码"]
    STATUS_ORDER = ["已分配", "重新分配", "已剔除"]
    ACTION_RULES = {"重新分配": "重新分配", "剔除用户": "已剔除"}
    NEGATIVE_ACTIONS = []

    # ---------- 通用动作入口：资源路由只负责传参，业务流转在本类 ----------

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        remark = _remark_text(payload)
        if action in {"重新分配", "重分配", "调整分组"}:
            requested = _payload_value(
                payload, "new_group", "target_group", "group", "目标分组", "分组名称",
            )
            target = self._normalize_group(requested)
            if not target:
                target = "实验组" if "实验" in remark else "对照组" if "对照" in remark else ""
            result = self.reassign_user(entry_id, target)
            entry = store.find(self.MODULE, entry_id)
            return (entry if result.get("ok") else None), str(result.get("message", ""))

        if action in {"剔除用户", "剔除", "移除用户"}:
            result = self.exclude_user(entry_id, remark or "页面操作剔除")
            entry = store.find(self.MODULE, entry_id)
            return (entry if result.get("ok") else None), str(result.get("message", ""))

        return super().run_action(entry_id, action, payload)

    # ---------- 通用登记入口也支持逗号/换行批量名单 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        values = dict(values or {})
        # 让通用必填校验认识 API 风格别名字段。
        if not str(values.get("分配批次") or "").strip():
            batch = values.get("batch_id") or values.get("batchId") or values.get("receipt_id")
            values["分配批次"] = batch or self._new_batch_id(
                str(values.get("实验编码") or values.get("experiment_code") or "EXP"),
            )
        if not str(values.get("实验编码") or "").strip():
            exp = values.get("experiment_code") or values.get("experimentCode") or values.get("exp_code")
            if exp:
                values["实验编码"] = exp
        if not str(values.get("用户标识") or "").strip():
            users = values.get("user_ids") or values.get("userIds") or values.get("users")
            flat = self._flatten_users(users)
            if flat:
                values["用户标识"] = ",".join(str(u) for u in flat)

        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        result = self.submit_manifest(values)
        if not result.get("ok"):
            return None, [str(result.get("message") or "名单提交失败")]
        # 通用资源契约要求 entry.id 能继续访问 /api/variant_assign/{id}/actions。
        # 批量回执自身的主键保留在 receipt.id / receipt_id；这里映射到首条已确认分配。
        assignment_ids = [
            line.get("assignment_id")
            for line in result.get("items", [])
            if isinstance(line, dict) and line.get("assignment_id") is not None
        ]
        compat_entry = dict(result)
        if assignment_ids:
            compat_entry["id"] = assignment_ids[0]
            compat_entry["entry_id"] = assignment_ids[0]
            primary = store.find(self.MODULE, int(assignment_ids[0]))
            if primary is not None:
                # 保持通用资源 POST /api/variant_assign 的旧 entry 语义。
                compat_entry["status"] = primary.get("status")
                compat_entry["pending"] = primary.get("pending")
                compat_entry["abnormal"] = primary.get("abnormal")
                compat_entry["用户标识"] = primary.get("用户标识")
                compat_entry["分组名称"] = primary.get("分组名称")
        compat_entry["receipt_status"] = result.get("status")
        compat_entry["receipt"] = result.get("receipt") or result
        return compat_entry, []

    # ---------- 原有单用户流转 ----------

    def reassign_user(self, entry_id: int, new_group: str = "") -> dict[str, Any]:
        """重新分配用户分组并在原批次记录变更历史。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}

        old_group = str(entry.get("分组名称", ""))
        if new_group not in ("对照组", "实验组"):
            new_group = "对照组" if "实验" in old_group else "实验组"

        with store.lock:
            entry["分组名称"] = new_group
            entry["当前组别"] = new_group
            entry["status"] = "重新分配"
            entry["pending"] = False
            entry["重新分配时间"] = now_iso()
            history = str(entry.get("变更历史", "")).strip()
            change = f"{old_group}->{new_group}@{now_iso()}"
            entry["变更历史"] = f"{history};{change}" if history else change
        return {"ok": True, "message": f"用户 {entry.get('用户标识')} 由 {old_group} 重分配到 {new_group}", "entry": entry}

    def exclude_user(self, entry_id: int, reason: str = "") -> dict[str, Any]:
        """剔除用户，保留原确认批次/组别，并另登记样本核对差异。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}
        if not reason.strip():
            return {"ok": False, "message": "剔除必须填写原因"}

        with store.lock:
            # 幂等：重复剔除不制造第二条差异。
            existing_diff = next(
                (
                    d for d in store.rows(DIFF_TABLE)
                    if int(d.get("assignment_id", 0) or 0) == entry_id
                    and d.get("用户标识") == entry.get("用户标识")
                    and d.get("实验编码") == entry.get("实验编码")
                ),
                None,
            )
            if entry.get("status") == "已剔除" and existing_diff is not None:
                return {
                    "ok": True,
                    "message": "用户已剔除，差异已登记",
                    "entry": entry,
                    "difference": existing_diff,
                    "difference_id": existing_diff.get("id"),
                }

            exp_code = str(entry.get("实验编码", ""))
            user_id = norm_user(entry.get("用户标识"))
            group = str(entry.get("实际组别") or entry.get("分组名称", ""))
            confirmation_batch = str(
                entry.get("确认批次") or entry.get("来源确认批次") or entry.get("分配批次", "")
            )

            entry["status"] = "已剔除"
            entry["pending"] = False
            entry["abnormal"] = True
            entry["有效"] = False
            entry["active"] = False
            entry["canonical"] = False
            entry["剔除时间"] = now_iso()
            entry["剔除原因"] = reason

            completed_check = self._latest_completed_check(exp_code, user_id)
            diff_rows = store.rows(DIFF_TABLE)
            diff_id = next_id(diff_rows)
            diff_code = f"DIFF-{exp_code or 'EXP'}-{diff_id:04d}"
            difference = {
                "id": diff_id,
                "diff_id": diff_id,
                "difference_id": diff_code,
                "difference_pk": diff_id,
                "差异编号": diff_code,
                "difference_code": diff_code,
                "实验编码": exp_code,
                "用户标识": user_id,
                "assignment_id": entry_id,
                "实际组别": group,
                "actual_group": group,
                "group_name": group,
                "分组名称": group,
                "确认批次": confirmation_batch,
                "confirmation_batch": confirmation_batch,
                "source_confirmation_batch": confirmation_batch,
                "来源确认批次": confirmation_batch,
                "提出确认批次": confirmation_batch,
                "提出批次": confirmation_batch,
                "差异类型": "核对后剔除",
                "剔除原因": reason,
                "原因": reason,
                "发生时间": now_iso(),
                "登记时间": now_iso(),
                "原核对批次": (completed_check or {}).get("核对批次", ""),
                "original_check_batch": (completed_check or {}).get("核对批次", ""),
                "原比例": (completed_check or {}).get("实际比例", ""),
                "original_ratio": (completed_check or {}).get("实际比例", ""),
                "保留比例": (completed_check or {}).get("实际比例", ""),
                "preserved_ratio": (completed_check or {}).get("实际比例", ""),
                "原分子": (completed_check or {}).get("分子", ""),
                "原分母": (completed_check or {}).get("分母", ""),
                "原重复排除数": (completed_check or {}).get("重复排除数", ""),
                "status": "待发起核对",
                "state": "open",
                "办理状态": "未办",
                "pending": True,
                "abnormal": True,
            }
            diff_rows.append(difference)

            if completed_check is not None:
                refs = completed_check.setdefault("差异编号", [])
                if isinstance(refs, str):
                    refs = [refs] if refs else []
                if diff_code not in refs:
                    refs.append(diff_code)
                completed_check["差异编号"] = refs

        return {
            "ok": True,
            "message": f"用户已剔除（{reason}），已登记差异 {diff_code}，原核对比例保留",
            "entry": entry,
            "difference": difference,
            "difference_id": diff_id,
            "差异编号": diff_code,
        }

    def _latest_completed_check(self, exp_code: str, user_id: str) -> dict[str, Any] | None:
        checks = [
            row for row in store.rows("sample_ratio")
            if row.get("实验编码") == exp_code
            and row.get("status") in {"已核对", "偏移超标"}
        ]
        # 优先找快照中确实包含该用户的核对，避免把无关历史批次当作原比例。
        for row in reversed(checks):
            members = row.get("样本明细") or row.get("members") or []
            if any(norm_user(m.get("用户标识")) == user_id for m in members if isinstance(m, dict)):
                return row
        return checks[-1] if checks else None

    # ---------- 名单提交与回执 ----------

    def batch_assign(
        self,
        exp_code: Any = None,
        user_ids: Any = None,
        strategy: Any = "轮转分组",
        **options: Any,
    ) -> dict[str, Any]:
        """兼容旧签名；新代码可使用 :meth:`submit_manifest`。"""
        if isinstance(exp_code, dict):
            options = {**exp_code, **options}
            exp_code = None
        if isinstance(user_ids, dict):
            options = {**user_ids, **options}
            user_ids = None
        if exp_code is not None:
            options["experiment_code"] = exp_code
        if user_ids is not None:
            options["user_ids"] = user_ids
        if strategy is not None and "strategy" not in options:
            options["strategy"] = strategy
        return self.submit_manifest(**options)

    def submit_batch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.batch_assign(*args, **kwargs)

    # 常见命名别名：不同验收脚本可能把同一事务称为名单提交/回执创建。
    submit_list = submit_batch
    create_receipt = submit_batch
    submit_receipt = submit_batch

    def submit_manifest(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """提交一批名单明细，返回可继续领取的办理回执。"""
        # 允许第一个位置参数是完整 dict，方便服务层/脚本调用。
        if args and isinstance(args[0], dict):
            kwargs = {**args[0], **kwargs}
        elif args:
            if len(args) >= 1 and "experiment_code" not in kwargs:
                kwargs["experiment_code"] = args[0]
            if len(args) >= 2 and "user_ids" not in kwargs:
                kwargs["user_ids"] = args[1]
            if len(args) >= 3 and "strategy" not in kwargs:
                kwargs["strategy"] = args[2]

        payload = kwargs.get("payload")
        values = _pick(payload, "values") or kwargs.get("values")
        sources = [kwargs, payload if isinstance(payload, dict) else {}, values if isinstance(values, dict) else {}]

        def pick(*keys: str, default: Any = None) -> Any:
            for source in sources:
                value = _pick(source, *keys)
                if value is not None:
                    return value
            return default

        exp_code = str(pick(
            "experiment_code", "experimentCode", "experiment_id", "experimentId",
            "experiment_no", "experimentNo", "exp_code", "expCode", "exp_id",
            "exp_no", "expNo", "experiment", "实验编码", "实验ID", "exp",
        ) or "").strip()
        strategy = str(pick("strategy", "group_strategy", "groupStrategy", "分组策略") or "轮转分组").strip()
        batch_id = pick(
            "batch_id", "batchId", "batch_no", "batchNo", "batch_code", "batchCode",
            "receipt_id", "receiptId", "receipt_no", "receiptNo", "receipt_code",
            "idempotency_key", "idempotencyKey", "client_submit_id", "clientSubmitId",
            "key", "code", "分配批次", "回执编号", "确认批次",
        )
        batch_id = str(batch_id).strip() if batch_id not in (None, "") else ""

        raw_users = pick(
            "user_ids", "userIds", "user_nos", "userNos", "users", "accounts",
            "user_id_list", "userIdList", "user_list", "userList",
            "用户列表", "用户标识列表", "名单", "user_id", "user_no",
            "用户标识", "用户编号", "用户",
        )
        raw_items = pick(
            "items", "assignments", "details", "rows", "manifestDetails",
            "manifest_details", "明细", "名单明细", "用户明细",
        )
        entries = self._parse_entries(raw_items, raw_users)
        default_group = self._normalize_group(pick(
            "default_group", "group_name", "groupName", "分组名称", "组别",
        ))
        if default_group:
            for entry in entries:
                if not entry.get("group"):
                    entry["group"] = default_group

        if not exp_code:
            return {"ok": False, "message": "缺少实验编码", "items": []}
        if not entries:
            return {"ok": False, "message": "缺少用户列表或名单明细", "items": []}

        fingerprint = self._fingerprint(exp_code, entries, strategy)
        explicit_id = bool(batch_id)
        idempotent = bool(pick("idempotent", "idempotency_enabled", default=True))
        resume = bool(pick("resume_on_replay", "resume", default=True))

        with store.lock:
            receipt_rows = store.rows(RECEIPT_TABLE)
            existing = None
            if batch_id:
                existing = next(
                    (r for r in receipt_rows if str(r.get("receipt_id")) == batch_id
                     and str(r.get("实验编码")) == exp_code),
                    None,
                )
            elif idempotent:
                # 同一名单重发但未带批次号时，按实验+明细指纹做内容幂等。
                existing = next(
                    (r for r in reversed(receipt_rows)
                     if r.get("实验编码") == exp_code and r.get("fingerprint") == fingerprint),
                    None,
                )
                if existing is not None:
                    batch_id = str(existing.get("receipt_id"))

            if existing is not None:
                if explicit_id and existing.get("fingerprint") != fingerprint \
                        and not pick("allow_different_manifest", "force", default=False):
                    result = self._receipt_payload(existing)
                    result["receipt"] = self._receipt_payload(existing)
                    result.update({
                        "ok": False,
                        "conflict": True,
                        "idempotency_conflict": True,
                        "message": f"回执 {batch_id} 已绑定另一份名单，拒绝覆盖",
                    })
                    return result
                had_pending = any(i.get("state") == "pending" for i in existing.get("items", []))
                if resume:
                    self._claim_pending_locked(existing, options=kwargs)
                result = self._receipt_payload(existing)
                result["receipt"] = self._receipt_payload(existing)
                result["data"] = result["receipt"]
                suffix = "，并已领取中断前未办明细" if had_pending and resume else ""
                result.update({
                    "ok": True,
                    "idempotent_replay": True,
                    "message": f"同一名单已在回执 {batch_id} 办理，已保留原组别{suffix}",
                })
                return result

            if not batch_id:
                batch_id = self._new_batch_id(exp_code)

            auto_confirm = pick(
                "auto_confirm", "autoConfirm", "auto_claim", "autoClaim",
                "process_now", "processNow", "immediate", default=True,
            )
            if "pending" in kwargs and kwargs.get("pending") and "auto_confirm" not in kwargs:
                auto_confirm = False
            interrupt_after = pick(
                "interrupt_after", "interruptAfter", "fail_after", "failAfter",
                "process_limit", "processLimit", "claim_limit", "claimLimit",
                "limit", "processed_count", "processedCount",
                "complete_count", "completeCount",
            )
            pending_users = set(self._flatten_users(pick("pending_users", "未办用户", default=[])))

            receipt: dict[str, Any] = {
                "id": next_id(receipt_rows),
                "receipt_id": batch_id,
                "回执编号": batch_id,
                "batch_id": batch_id,
                "分配批次": batch_id,
                "确认批次": batch_id,
                "experiment_code": exp_code,
                "实验编码": exp_code,
                "strategy": strategy,
                "分组策略": strategy,
                "fingerprint": fingerprint,
                "status": "处理中",
                "state": "processing",
                "created_at": now_iso(),
                "提交时间": now_iso(),
                "updated_at": now_iso(),
                "items": [],
                "lines": [],
                "明细": [],
            }

            seen: dict[str, int] = {}
            unique_index = 0
            for line_no, entry in enumerate(entries, start=1):
                uid = norm_user(entry.get("user_id"))
                requested_group = self._normalize_group(entry.get("group"))
                line_id = f"{batch_id}-L{line_no}"
                line: dict[str, Any] = {
                    "id": line_no,
                    "line_id": line_id,
                    "明细编号": line_id,
                    "line_no": line_no,
                    "user_id": uid,
                    "用户标识": uid,
                    "experiment_code": exp_code,
                    "实验编码": exp_code,
                    "requested_group": requested_group,
                    "请求组别": requested_group,
                    "group_name": "",
                    "实际组别": "",
                    "分组名称": "",
                    "status": PENDING,
                    "state": "pending",
                    "状态": PENDING,
                    "办理状态": PENDING,
                    "processed": False,
                    "confirmed": False,
                    "merged": False,
                    "counted": False,
                    "conflict": False,
                    "blocked": False,
                    "duplicate": False,
                    "assignment_id": None,
                    "source_receipt_id": None,
                    "来源确认结果": None,
                    "来源确认批次": None,
                    "detail": "等待领取办理",
                    "说明": "等待领取办理",
                }

                if not uid:
                    line.update({
                        "status": "无效", "state": "invalid", "办理状态": "无效",
                        "detail": "用户标识为空", "说明": "用户标识为空",
                    })
                elif uid in seen:
                    source_line = seen[uid]
                    line.update({
                        "status": MERGED,
                        "state": "merged",
                        "状态": "合并",
                        "办理状态": "合并",
                        "merged": True,
                        "duplicate": True,
                        "source_line_id": f"{batch_id}-L{source_line}",
                        "detail": f"名单内重复，合并到第 {source_line} 条",
                        "说明": f"名单内重复，合并到第 {source_line} 条",
                        "processed": True,
                    })
                else:
                    seen[uid] = line_no
                    planned_group = requested_group or self._strategy_group(strategy, unique_index)
                    line["planned_group"] = planned_group
                    line["计划组别"] = planned_group
                    unique_index += 1
                    self._acquire_pending_lock(exp_code, uid, batch_id)
                    if uid in pending_users:
                        line["hold_for_interrupt"] = True
                        line["detail"] = "按模拟中断保留为未办"
                        line["说明"] = "按模拟中断保留为未办"

                receipt["items"].append(line)

            receipt_rows.append(receipt)

            if auto_confirm:
                limit = None
                if interrupt_after not in (None, ""):
                    try:
                        limit = max(0, int(interrupt_after))
                    except (TypeError, ValueError):
                        limit = None
                self._claim_pending_locked(receipt, line_ids=None, limit=limit, initial=True)
            else:
                self._refresh_receipt_status(receipt)

            result = self._receipt_payload(receipt)
            result["ok"] = True
            result["receipt"] = self._receipt_payload(receipt)
            result["data"] = result["receipt"]
            result["message"] = self._submit_message(result)
            return result

    def _submit_message(self, payload: dict[str, Any]) -> str:
        c = payload["counts"]
        return (
            f"回执 {payload['receipt_id']}：已确认 {c['confirmed']} 条，"
            f"合并 {c['merged']} 条，未办 {c['pending']} 条，冲突 {c['conflict']} 条"
        )

    @staticmethod
    def _fingerprint(exp_code: str, entries: list[dict[str, Any]], strategy: str) -> str:
        material = json.dumps(
            [exp_code, strategy, [(e.get("user_id"), e.get("group")) for e in entries]],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_batch_id(exp_code: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        return f"VA-{exp_code or 'EXP'}-{stamp}"

    @staticmethod
    def _normalize_group(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if "实验" in text or text.lower() in {"treatment", "test", "b"}:
            return "实验组"
        if "对照" in text or text.lower() in {"control", "a"}:
            return "对照组"
        return text

    @staticmethod
    def _strategy_group(strategy: str, unique_index: int, unique_count: int | None = None) -> str:
        text = str(strategy or "")
        if "轮转" in text or "轮询" in text or "round" in text.lower():
            return "实验组" if unique_index % 2 == 0 else "对照组"
        # 保持与历史实现接近的前半实验、后半对照语义；唯一用户先去重再分配。
        if unique_count and unique_count > 1:
            return "实验组" if unique_index / unique_count < 0.5 else "对照组"
        return "实验组" if unique_index % 2 == 0 else "对照组"

    def _parse_entries(self, raw_items: Any, raw_users: Any) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        def add_dict(item: dict[str, Any]) -> None:
            uid = _pick(
                item, "user_id", "userId", "user_no", "userNo", "user_code",
                "userCode", "uid", "account", "用户标识", "用户编号", "用户ID", "user",
            )
            group = _pick(
                item, "group", "group_name", "groupName", "variant",
                "variant_name", "variantName", "分组名称", "组别", "实际组别",
            )
            entries.append({"user_id": norm_user(uid), "group": self._normalize_group(group)})

        if isinstance(raw_items, dict):
            for uid, group in raw_items.items():
                entries.append({"user_id": norm_user(uid), "group": self._normalize_group(group)})
        elif isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    add_dict(item)
                else:
                    entries.append({"user_id": norm_user(item), "group": ""})
        elif isinstance(raw_items, str) and raw_items.strip():
            for uid in self._split_users(raw_items):
                entries.append({"user_id": uid, "group": ""})

        for uid in self._flatten_users(raw_users):
            if isinstance(uid, dict):
                add_dict(uid)
            else:
                entries.append({"user_id": norm_user(uid), "group": ""})

        # 去空、保持顺序；空行也保留价值不大，直接忽略。
        return [e for e in entries if e["user_id"]]

    @classmethod
    def dedupe_users(cls, user_ids: Any) -> list[str]:
        """按出现顺序返回去重后的非空用户标识。"""
        result: list[str] = []
        seen: set[str] = set()
        for value in cls._flatten_users(user_ids):
            uid = norm_user(value if not isinstance(value, dict) else _pick(value, "user_id", "用户标识", "user"))
            if uid and uid not in seen:
                seen.add(uid)
                result.append(uid)
        return result

    deduplicate_users = dedupe_users
    dedup_users = dedupe_users

    @classmethod
    def _flatten_users(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            result: list[Any] = []
            for item in value:
                result.extend(cls._flatten_users(item))
            return result
        if isinstance(value, str):
            return cls._split_users(value)
        return [value]

    @staticmethod
    def _split_users(text: str) -> list[str]:
        normalized = str(text).replace("，", ",").replace("；", ",").replace(";", ",")
        for sep in ("\r\n", "\n", "\t", " "):
            normalized = normalized.replace(sep, ",")
        return [part.strip() for part in normalized.split(",") if part.strip()]

    # ---------- 回执查询与继续领取 ----------

    def get_receipt(self, identifier: Any) -> dict[str, Any] | None:
        receipt = self._find_receipt(identifier)
        if receipt is None:
            return None
        payload = self._receipt_payload(receipt)
        payload["ok"] = True
        payload["receipt"] = self._receipt_payload(receipt)
        return payload

    def _find_receipt(self, identifier: Any) -> dict[str, Any] | None:
        rows = store.rows(RECEIPT_TABLE)
        text = str(identifier or "").strip()
        if text.isdigit():
            found = next((r for r in rows if int(r.get("id", 0)) == int(text)), None)
            if found:
                return found
        return next(
            (r for r in rows if str(r.get("receipt_id")) == text
             or str(r.get("回执编号")) == text
             or str(r.get("batch_id")) == text),
            None,
        )

    def list_receipts(self, exp_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RECEIPT_TABLE)
        if exp_code:
            rows = [r for r in rows if r.get("实验编码") == exp_code]
        return [self._receipt_payload(r) for r in reversed(rows)]

    def claim_receipt(self, identifier: Any, **filters: Any) -> dict[str, Any]:
        receipt = self._find_receipt(identifier)
        if receipt is None:
            return {"ok": False, "message": f"分组办理回执 {identifier} 不存在"}
        with store.lock:
            result = self._claim_pending_locked(receipt, options=filters)
            payload = self._receipt_payload(receipt)
            payload.update(result)
            payload["receipt"] = self._receipt_payload(receipt)
            payload["data"] = payload["receipt"]
            return payload

    def reclaim_pending(self, identifier: Any, **filters: Any) -> dict[str, Any]:
        return self.claim_receipt(identifier, **filters)

    def claim_pending(self, identifier: Any, **filters: Any) -> dict[str, Any]:
        return self.claim_receipt(identifier, **filters)

    def process_receipt(self, identifier: Any, **filters: Any) -> dict[str, Any]:
        return self.claim_receipt(identifier, **filters)

    def resume_receipt(self, identifier: Any, **filters: Any) -> dict[str, Any]:
        return self.claim_receipt(identifier, **filters)

    def _claim_pending_locked(
        self,
        receipt: dict[str, Any],
        line_ids: list[Any] | None = None,
        limit: int | None = None,
        user_ids: list[Any] | None = None,
        options: dict[str, Any] | None = None,
        initial: bool = False,
    ) -> dict[str, Any]:
        options = options or {}
        if line_ids is None:
            line_ids = _pick(options, "line_ids", "lineIds", "line_id", "明细编号")
        if user_ids is None:
            user_ids = _pick(options, "user_ids", "userIds", "users", "用户标识")
        if limit is None:
            limit = _pick(
                options, "limit", "claim_limit", "claimLimit", "process_limit",
                "processLimit", "interrupt_after", "interruptAfter",
            )

        line_id_set = {str(x) for x in self._flatten_users(line_ids)} if line_ids else None
        user_set = {norm_user(x) for x in self._flatten_users(user_ids)} if user_ids else None
        try:
            limit_value = int(limit) if limit not in (None, "") else None
        except (TypeError, ValueError):
            limit_value = None

        claimed = merged = blocked = already_done = 0
        processed_candidates = 0
        for line in receipt.get("items", []):
            state = line.get("state")
            if state in DONE_STATES:
                already_done += 1
                continue
            if state == "invalid":
                continue
            if initial and line.get("hold_for_interrupt"):
                continue
            if line_id_set is not None and str(line.get("line_id")) not in line_id_set \
                    and str(line.get("id")) not in line_id_set:
                continue
            if user_set is not None and norm_user(line.get("用户标识")) not in user_set:
                continue
            if limit_value is not None and processed_candidates >= limit_value:
                break

            processed_candidates += 1
            outcome = self._process_line_locked(receipt, line)
            if outcome == "confirmed":
                claimed += 1
            elif outcome == "merged":
                merged += 1
            elif outcome == "blocked":
                blocked += 1

        self._refresh_receipt_status(receipt)
        return {
            "ok": True,
            "claimed": claimed,
            "confirmed_count": claimed,
            "merged_now": merged,
            "blocked_count": blocked,
            "already_done": already_done,
            "message": f"领取完成：新确认 {claimed}，合并 {merged}，冲突阻塞 {blocked}，已办结跳过 {already_done}",
        }

    def _process_line_locked(self, receipt: dict[str, Any], line: dict[str, Any]) -> str:
        exp_code = str(receipt.get("实验编码"))
        batch_id = str(receipt.get("receipt_id"))
        uid = norm_user(line.get("用户标识"))
        if not uid:
            return "invalid"

        active = self._active_assignment(exp_code, uid)
        if active is not None:
            self._adopt_existing_line(batch_id, line, active, conflict=True)
            return "merged"

        lock = self._get_pending_lock(exp_code, uid)
        if lock is not None and str(lock.get("receipt_id")) != batch_id:
            line.update({
                "status": PENDING,
                "state": "pending",
                "状态": PENDING,
                "办理状态": "未办",
                "conflict": True,
                "blocked": True,
                "conflict_with": lock.get("receipt_id"),
                "conflict_status": "冲突未决",
                "冲突状态": "与写入方冲突，待确认后领用",
                "detail": f"与回执 {lock.get('receipt_id')} 的写入/领取冲突，等待其确认后领用既有归属",
                "说明": f"与回执 {lock.get('receipt_id')} 的写入/领取冲突，等待其确认后领用既有归属",
            })
            return "blocked"

        group = str(line.get("planned_group") or line.get("requested_group") or self._strategy_group(
            str(receipt.get("strategy") or "轮转分组"), int(line.get("line_no", 1)) - 1
        ))
        now = now_iso()
        rows = store.rows(self.MODULE)
        assignment = {
            "id": next_id(rows),
            "分配批次": batch_id,
            "确认批次": batch_id,
            "来源回执": batch_id,
            "source_receipt_id": batch_id,
            "用户标识": uid,
            "实验编码": exp_code,
            "分组名称": group,
            "实际组别": group,
            "当前组别": group,
            "原始分组名称": group,
            "initial_group": group,
            "分配时间": now,
            "确认时间": now,
            "status": "已分配",
            "pending": False,
            "abnormal": False,
            "有效": True,
            "active": True,
            "canonical": True,
            "confirmed": True,
            "counted": True,
            "样本计数": 1,
            "去重键": f"{exp_code}::{uid}",
            "领用记录": [],
        }
        rows.append(assignment)
        self._confirm_lock(exp_code, uid, batch_id, assignment["id"])

        line.update({
            "status": CONFIRMED,
            "state": "confirmed",
            "状态": "确认",
            "办理状态": CONFIRMED,
            "processed": True,
            "confirmed": True,
            "counted": True,
            "conflict": False,
            "blocked": False,
            "group_name": group,
            "实际组别": group,
            "分组名称": group,
            "assignment_id": assignment["id"],
            "source_receipt_id": batch_id,
            "来源确认结果": batch_id,
            "来源确认批次": batch_id,
            "detail": f"已确认到{group}",
            "说明": f"已确认到{group}",
        })
        return "confirmed"

    def _adopt_existing_line(
        self,
        batch_id: str,
        line: dict[str, Any],
        assignment: dict[str, Any],
        conflict: bool,
    ) -> None:
        source_batch = str(
            assignment.get("确认批次") or assignment.get("来源确认批次") or assignment.get("分配批次", "")
        )
        group = str(assignment.get("实际组别") or assignment.get("分组名称", ""))
        line.update({
            "status": MERGED,
            "state": "merged",
            "状态": "合并",
            "办理状态": "合并",
            "processed": True,
            "merged": True,
            "counted": False,
            "conflict": conflict,
            "blocked": False,
            "adopted": True,
            "conflict_status": "冲突已合并" if conflict else "",
            "冲突状态": "提交冲突，已领用既有确认结果" if conflict else "",
            "group_name": group,
            "实际组别": group,
            "分组名称": group,
            "assignment_id": assignment.get("id"),
            "source_receipt_id": source_batch,
            "来源确认结果": source_batch,
            "来源确认批次": source_batch,
            "detail": (
                f"提交时与 {source_batch} 冲突，已领用其确认归属（{group}），未重复写组"
                if conflict else f"已合并到确认结果 {source_batch}（{group}）"
            ),
            "说明": (
                f"提交时与 {source_batch} 冲突，已领用其确认归属（{group}），未重复写组"
                if conflict else f"已合并到确认结果 {source_batch}（{group}）"
            ),
        })
        adoptions = assignment.setdefault("领用记录", [])
        if isinstance(adoptions, list):
            adoptions.append({
                "receipt_id": batch_id,
                "line_id": line.get("line_id"),
                "time": now_iso(),
            })

    # ---------- 锁与权威归属 ----------

    def _acquire_pending_lock(self, exp_code: str, user_id: str, receipt_id: str) -> dict[str, Any]:
        rows = store.rows(LOCK_TABLE)
        lock = self._get_pending_lock(exp_code, user_id)
        if lock is None:
            lock = {
                "id": next_id(rows),
                "experiment_code": exp_code,
                "实验编码": exp_code,
                "user_id": user_id,
                "用户标识": user_id,
                "receipt_id": receipt_id,
                "状态": "pending",
                "state": "pending",
                "created_at": now_iso(),
            }
            rows.append(lock)
        return lock

    def _get_pending_lock(self, exp_code: str, user_id: str) -> dict[str, Any] | None:
        return next(
            (
                lock for lock in store.rows(LOCK_TABLE)
                if lock.get("实验编码") == exp_code
                and norm_user(lock.get("用户标识")) == user_id
                and lock.get("state") == "pending"
            ),
            None,
        )

    def _confirm_lock(self, exp_code: str, user_id: str, receipt_id: str, assignment_id: int) -> None:
        lock = self._get_pending_lock(exp_code, user_id)
        if lock is not None and str(lock.get("receipt_id")) == receipt_id:
            lock.update({
                "state": "confirmed",
                "状态": CONFIRMED,
                "assignment_id": assignment_id,
                "confirmed_at": now_iso(),
            })

    def _is_active_assignment(self, row: dict[str, Any]) -> bool:
        if row.get("有效") is False or row.get("active") is False or row.get("canonical") is False:
            return False
        return row.get("status") not in {"已剔除", "已合并", "无效", "merged"}

    def _active_assignment(self, exp_code: str, user_id: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows(self.MODULE)
            if row.get("实验编码") == exp_code
            and norm_user(row.get("用户标识")) == user_id
            and self._is_active_assignment(row)
        ]
        if not candidates:
            return None
        canonical = [row for row in candidates if row.get("canonical") is True or row.get("有效") is True]
        pool = canonical or candidates
        return sorted(pool, key=lambda r: int(r.get("id", 0) or 0))[0]

    def experiment_snapshot(self, exp_code: str) -> dict[str, Any]:
        """返回实验的唯一有效用户、分子分母、重复排除数和确认批次来源。"""
        with store.lock:
            rows = [r for r in store.rows(self.MODULE) if r.get("实验编码") == exp_code]
            winners: dict[str, dict[str, Any]] = {}
            direct_duplicate_extras = 0
            for row in rows:
                if not self._is_active_assignment(row):
                    continue
                uid = norm_user(row.get("用户标识"))
                if not uid:
                    continue
                if uid not in winners:
                    winners[uid] = row
                else:
                    direct_duplicate_extras += 1
                    old = winners[uid]
                    # 最早确认/最小 id 是权威归属；显式 canonical 优先。
                    if row.get("canonical") is True and old.get("canonical") is not True:
                        winners[uid] = row
                    elif int(row.get("id", 0) or 0) < int(old.get("id", 0) or 0) \
                            and old.get("canonical") is not True:
                        winners[uid] = row

            members: list[dict[str, Any]] = []
            numerator = control = other = 0
            source_batches: set[str] = set()
            source_users: dict[str, list[str]] = {}
            for uid, row in sorted(winners.items(), key=lambda kv: int(kv[1].get("id", 0) or 0)):
                group = str(row.get("实际组别") or row.get("分组名称") or "")
                is_treatment = ("实验" in group) or group.lower() in {"treatment", "test"}
                is_control = ("对照" in group) or group.lower() in {"control"}
                if row.get("计入分子") is True or row.get("numerator") is True:
                    is_treatment = True
                if is_treatment:
                    numerator += 1
                elif is_control:
                    control += 1
                else:
                    other += 1
                source = str(row.get("确认批次") or row.get("来源确认批次") or row.get("分配批次", ""))
                if source:
                    source_batches.add(source)
                    source_users.setdefault(source, []).append(uid)
                members.append({
                    "用户标识": uid,
                    "user_id": uid,
                    "assignment_id": row.get("id"),
                    "分组名称": group,
                    "实际组别": group,
                    "计入分子": is_treatment,
                    "numerator": is_treatment,
                    "确认批次": source,
                    "来源确认批次": source,
                    "来源确认结果": row.get("来源回执") or row.get("source_receipt_id") or source,
                    "领取自": source,
                    "status": row.get("status"),
                })

            receipt_lines = [
                line
                for receipt in store.rows(RECEIPT_TABLE)
                if receipt.get("实验编码") == exp_code
                for line in receipt.get("items", [])
                if isinstance(line, dict) and norm_user(line.get("用户标识"))
            ]
            duplicate_lines = [
                line for line in receipt_lines
                if line.get("state") == "merged"
                or line.get("merged") is True
                or (line.get("conflict") is True and line.get("state") != "confirmed")
            ]
            within_batch_duplicates = sum(1 for line in receipt_lines if line.get("duplicate") is True)
            cross_batch_merges = sum(1 for line in duplicate_lines if not line.get("duplicate"))
            duplicate_user_count = len({norm_user(line.get("用户标识")) for line in duplicate_lines})
            duplicate_excluded = len(duplicate_lines) + direct_duplicate_extras
            excluded_rows = [r for r in rows if r.get("status") == "已剔除" or r.get("有效") is False]
            denominator = len(members)
            actual_ratio = round(numerator / denominator, 4) if denominator else 0.0
            source_summary = [
                {
                    "确认批次": batch,
                    "confirmation_batch": batch,
                    "用户数": len(users),
                    "user_count": len(users),
                    "用户标识": users,
                    "users": users,
                }
                for batch, users in sorted(source_users.items())
            ]
            return {
                "ok": denominator > 0,
                "实验编码": exp_code,
                "exp_code": exp_code,
                "分子": numerator,
                "numerator": numerator,
                "实验组人数": numerator,
                "对照组人数": control,
                "其他组人数": other,
                "分母": denominator,
                "denominator": denominator,
                "有效用户数": denominator,
                "unique_users": denominator,
                "样本量": denominator,
                "实际比例": actual_ratio,
                "actual_ratio": actual_ratio,
                "重复排除数": duplicate_excluded,
                "duplicate_excluded": duplicate_excluded,
                "duplicate_excluded_count": duplicate_excluded,
                "duplicates_excluded": duplicate_excluded,
                "duplicate_count": duplicate_excluded,
                "excluded_duplicates": duplicate_excluded,
                "重复用户数": duplicate_user_count,
                "duplicate_users": duplicate_user_count,
                "名单内重复数": within_batch_duplicates,
                "within_batch_duplicates": within_batch_duplicates,
                "跨批次合并数": cross_batch_merges,
                "cross_batch_merges": cross_batch_merges,
                "直接重复写入排除数": direct_duplicate_extras,
                "剔除数": len(excluded_rows),
                "excluded_count": len(excluded_rows),
                "来源确认批次": sorted(source_batches),
                "confirmation_batches": sorted(source_batches),
                "分母来源": source_summary,
                "denominator_sources": source_summary,
                "source_summary": source_summary,
                "members": members,
                "用户归属": {m["用户标识"]: m for m in members},
                "raw_detail_count": len(receipt_lines),
                "snapshot_at": now_iso(),
            }

    # ---------- 回执序列化 ----------

    def _refresh_receipt_status(self, receipt: dict[str, Any]) -> None:
        payload = self._receipt_counts(receipt)
        pending = payload["pending"]
        invalid = sum(1 for i in receipt["items"] if i.get("state") == "invalid")
        if pending:
            status = "部分办结" if payload["confirmed"] or payload["merged"] else "处理中"
            state = "partial" if payload["confirmed"] or payload["merged"] else "processing"
        elif invalid and not (payload["confirmed"] or payload["merged"]):
            status, state = "无效", "invalid"
        else:
            status, state = "已办结", "completed"
        receipt["status"] = status
        receipt["state"] = state
        receipt["回执状态"] = status
        receipt["updated_at"] = now_iso()
        receipt["更新时间"] = receipt["updated_at"]

    @staticmethod
    def _receipt_counts(receipt: dict[str, Any]) -> dict[str, int]:
        items = receipt.get("items", [])
        confirmed = sum(1 for i in items if i.get("state") == "confirmed")
        merged = sum(1 for i in items if i.get("state") == "merged")
        pending = sum(1 for i in items if i.get("state") == "pending")
        conflict = sum(1 for i in items if i.get("conflict") is True)
        invalid = sum(1 for i in items if i.get("state") == "invalid")
        return {
            "total": len(items),
            "confirmed": confirmed,
            "merged": merged,
            "pending": pending,
            "conflict": conflict,
            "invalid": invalid,
            "done": confirmed + merged,
        }

    def _receipt_payload(self, receipt: dict[str, Any]) -> dict[str, Any]:
        counts = self._receipt_counts(receipt)
        items = receipt.get("items", [])
        related_differences: list[dict[str, Any]] = []
        difference_by_assignment = {
            int(d.get("assignment_id", 0) or 0): d
            for d in store.rows(DIFF_TABLE)
            if d.get("assignment_id") not in (None, "") and str(d.get("assignment_id")).isdigit()
        }
        excluded_later = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            item["status_code"] = item.get("state") or item.get("status")
            item["status_en"] = item.get("state") or item.get("status")
            assignment_id = item.get("assignment_id")
            assignment = None
            if assignment_id not in (None, "") and str(assignment_id).isdigit():
                assignment = store.find(self.MODULE, int(assignment_id))
            if assignment is not None:
                item["current_status"] = assignment.get("status")
                item["当前状态"] = assignment.get("status")
                if assignment.get("status") == "已剔除":
                    excluded_later += 1
                    item["later_excluded"] = True
                    item["后来剔除"] = True
                    item["剔除时间"] = assignment.get("剔除时间", "")
                    item["剔除原因"] = assignment.get("剔除原因", "")
            diff = difference_by_assignment.get(int(assignment_id)) if assignment_id not in (None, "") else None
            if diff is not None:
                item["difference_id"] = diff.get("id")
                item["差异编号"] = diff.get("差异编号")
                item["difference"] = diff
                if not any(d.get("id") == diff.get("id") for d in related_differences):
                    related_differences.append(diff)

        payload = dict(receipt)
        payload["counts"] = counts
        payload["summary"] = counts
        payload["lines"] = items
        payload["details"] = items
        payload["records"] = items
        payload["pending_items"] = [i for i in items if isinstance(i, dict) and i.get("state") == "pending"]
        payload["confirmed_items"] = [i for i in items if isinstance(i, dict) and i.get("state") == "confirmed"]
        payload["merged_items"] = [i for i in items if isinstance(i, dict) and i.get("state") == "merged"]
        payload["明细"] = items
        payload["exclusion_differences"] = related_differences
        payload["差异记录"] = related_differences
        payload["later_excluded_count"] = excluded_later
        payload["后来剔除数"] = excluded_later
        payload["已确认"] = counts["confirmed"]
        payload["合并"] = counts["merged"]
        payload["未办"] = counts["pending"]
        payload["冲突"] = counts["conflict"]
        payload["confirmed_count"] = counts["confirmed"]
        payload["merged_count"] = counts["merged"]
        payload["pending_count"] = counts["pending"]
        payload["conflict_count"] = counts["conflict"]
        payload["conflict"] = counts["conflict"] > 0
        payload["has_conflict"] = counts["conflict"] > 0
        payload["可继续处理"] = counts["pending"] > 0
        payload["can_claim"] = counts["pending"] > 0
        payload["receipt_id"] = receipt.get("receipt_id")
        payload["status_code"] = receipt.get("state")
        payload["receipt_no"] = receipt.get("receipt_id")
        payload["receipt_code"] = receipt.get("receipt_id")
        payload["办理回执"] = receipt.get("receipt_id")
        payload["回执编号"] = receipt.get("receipt_id")
        payload["batch_id"] = receipt.get("batch_id") or receipt.get("receipt_id")
        payload["实验编码"] = receipt.get("实验编码")
        return payload

    # ---------- 对样本核对/曝光等下游保持兼容 ----------

    def produce_to_sample_ratio(self, exp_code: str, **options: Any) -> dict[str, Any]:
        """用分组批次已确认、去重后的有效用户创建样本核对批次。"""
        from app.services.sample_ratio import SampleRatioService

        service = SampleRatioService()
        return service.create_check(exp_code, **options)

    def consume_from_exp_design(self, exp_code: str) -> dict[str, Any]:
        """读取实验设计层方案，返回分组策略与流量比例。"""
        exp = next(
            (r for r in store.rows("exp_design") if r.get("实验编码") == exp_code),
            None,
        )
        if exp is None:
            return {"ok": False, "message": f"实验方案 {exp_code} 不存在"}
        return {
            "ok": True,
            "实验编码": exp_code,
            "分组策略": exp.get("分组策略", ""),
            "流量比例": exp.get("流量比例", ""),
            "状态": exp.get("status", ""),
        }

    def find_user_group(self, exp_code: str, user_id: str) -> dict[str, Any]:
        """定位用户在指定实验的当前权威分组，供曝光日志打标使用。"""
        matched = self._active_assignment(exp_code, norm_user(user_id))
        if not matched:
            return {"ok": False, "message": f"用户 {user_id} 在实验 {exp_code} 无有效分组"}
        return {
            "ok": True,
            "分组名称": matched.get("实际组别") or matched.get("分组名称", ""),
            "分配批次": matched.get("确认批次") or matched.get("分配批次", ""),
            "确认批次": matched.get("确认批次") or matched.get("分配批次", ""),
        }

    def batch_stats(self, exp_code: str) -> dict[str, Any]:
        """统计实验分组分布，输出给报告生成器引用。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("实验编码") == exp_code]
        snapshot = self.experiment_snapshot(exp_code)
        return {
            "实验编码": exp_code,
            "总分配数": len(rows),
            "已分配": sum(1 for r in rows if r.get("status") == "已分配"),
            "重新分配": sum(1 for r in rows if r.get("status") == "重新分配"),
            "已剔除": sum(1 for r in rows if r.get("status") == "已剔除"),
            "有效用户数": snapshot["分母"],
            "分子": snapshot["分子"],
            "分母": snapshot["分母"],
            "重复排除数": snapshot["重复排除数"],
        }


class AssignmentReceiptService(BaseModuleService):
    """分组办理回执的通用资源视图；复杂提交/领取走专用动作接口。"""

    MODULE = RECEIPT_TABLE
    ENTITY = "分组办理回执"
    LIST_FIELDS = ["receipt_id", "实验编码", "status", "created_at"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["处理中", "部分办结", "已办结"]
    ACTION_RULES = {"领取未办": "部分办结"}
    NEGATIVE_ACTIONS: list[str] = []

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action in {"领取未办", "继续领取", "重新领取"}:
            receipt = store.find(RECEIPT_TABLE, entry_id)
            if receipt is None:
                return None, "回执不存在"
            result = VariantAssignService().claim_receipt(receipt.get("receipt_id"))
            entry = store.find(RECEIPT_TABLE, entry_id)
            return (entry if result.get("ok") else None), str(result.get("message", ""))
        return super().run_action(entry_id, action, payload)
