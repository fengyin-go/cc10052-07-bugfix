"""样本量核对领域服务。

核对批次的分子/分母来自分组分配器中“分组批次已确认且当前有效”的唯一用户：
同一实验同一用户只贡献一个样本，跨实验分别计算。核对办结时保存不可变快照，
之后用户被剔除只登记差异并可发起新核对，不回写原比例。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.variant_assign import DIFF_TABLE, VariantAssignService, next_id
from app.store import store


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SampleRatioService(BaseModuleService):
    MODULE = "sample_ratio"
    ENTITY = "核对结果"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["核对批次", "实验编码", "计划比例", "实际比例", "偏移量"]
    REQUIRED_FIELDS = ["核对批次", "实验编码", "计划比例"]
    STATUS_ORDER = ["待核对", "已核对", "偏移超标"]
    ACTION_RULES = {"执行核对": "已核对", "告警偏移": "偏移超标"}
    NEGATIVE_ACTIONS = []

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action in {"执行核对", "运行核对", "计算核对", "重新计算"}:
            result = self.run_check(entry_id)
            entry = store.find(self.MODULE, entry_id)
            return (entry if result.get("ok") else None), str(result.get("message", ""))
        if action in {"告警偏移", "登记告警"}:
            result = self.alert_offset(entry_id)
            entry = store.find(self.MODULE, entry_id)
            return (entry if result.get("ok") else None), str(result.get("message", ""))
        if action in {"发起新核对", "据差异发起新核对", "发起核对"}:
            # 兼容把差异 id 直接发到 /api/sample_ratio/{id}/actions 的调用。
            difference = self.get_difference(entry_id)
            if difference is not None:
                result = self.create_check_from_difference(entry_id)
                return (result.get("entry") if result.get("ok") else None), str(result.get("message", ""))
            check = store.find(self.MODULE, entry_id)
            if check is not None:
                result = self.create_check(str(check.get("实验编码")))
                return (result.get("entry") if result.get("ok") else None), str(result.get("message", ""))
        return super().run_action(entry_id, action, payload)

    # ---------- 创建/计算核对批次 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        exp_code = str(entry.get("实验编码") or "").strip()
        if exp_code:
            snapshot = VariantAssignService().experiment_snapshot(exp_code)
            if snapshot.get("分母"):
                entry.setdefault("分子", snapshot["分子"])
                entry.setdefault("numerator", snapshot["numerator"])
                entry.setdefault("分母", snapshot["分母"])
                entry.setdefault("denominator", snapshot["denominator"])
                entry.setdefault("重复排除数", snapshot["重复排除数"])
                entry.setdefault("duplicate_excluded", snapshot["duplicate_excluded"])
                entry.setdefault("来源确认批次", snapshot["来源确认批次"])
                entry.setdefault("样本明细", copy.deepcopy(snapshot["members"]))
        return entry, []

    def create_check(
        self,
        exp_code: Any = None,
        planned_ratio: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        """从分组确认结果重算样本，并登记一个新的样本核对批次。"""
        if isinstance(exp_code, dict):
            options = {**exp_code, **options}
            exp_code = (
                options.get("experiment_code") or options.get("experimentCode")
                or options.get("experiment_id") or options.get("experimentId")
                or options.get("experiment_no") or options.get("experimentNo")
                or options.get("exp_code") or options.get("expCode")
                or options.get("exp_no") or options.get("expNo")
                or options.get("实验编码")
            )
            planned_ratio = planned_ratio if planned_ratio is not None else options.get("planned_ratio")

        exp_code = str(
            exp_code
            or options.get("experiment_code")
            or options.get("experimentCode")
            or options.get("experiment_id")
            or options.get("experimentId")
            or options.get("experiment_no")
            or options.get("experimentNo")
            or options.get("exp_code")
            or options.get("expCode")
            or options.get("exp_no")
            or options.get("expNo")
            or options.get("实验编码")
            or ""
        ).strip()
        if not exp_code:
            return {"ok": False, "message": "缺少实验编码"}

        source_receipt = str(
            options.get("source_receipt_id")
            or options.get("sourceReceiptId")
            or options.get("receipt_id")
            or options.get("receiptId")
            or options.get("batch_id")
            or options.get("batchId")
            or options.get("回执编号")
            or options.get("确认批次")
            or options.get("来源回执")
            or ""
        ).strip()

        snapshot = VariantAssignService().experiment_snapshot(exp_code)
        if int(snapshot.get("分母", 0)) <= 0:
            return {"ok": False, "message": f"实验 {exp_code} 无已确认的有效分组用户", **snapshot}

        if planned_ratio is None:
            planned_ratio = options.get("计划比例")
        planned = self._parse_ratio(planned_ratio)
        if planned is None:
            design = self.consume_from_exp_design(exp_code)
            planned = self._parse_ratio(design.get("计划比例")) if design.get("ok") else 0.5
        planned = planned if planned is not None else 0.5

        ratio_rows = store.rows(self.MODULE)
        check_code = f"SR-{exp_code}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        offset = round(abs(float(snapshot["实际比例"]) - planned), 4)
        source_batches = snapshot.get("来源确认批次", [])
        entry: dict[str, Any] = {
            "id": next_id(ratio_rows),
            "核对批次": check_code,
            "check_batch": check_code,
            "check_no": check_code,
            "sample_check_batch": check_code,
            "batch_id": check_code,
            "实验编码": exp_code,
            "exp_code": exp_code,
            "计划比例": f"{planned:.4f}",
            "planned_ratio": planned,
            "实际比例": str(snapshot["实际比例"]),
            "actual_ratio": snapshot["实际比例"],
            "偏移量": str(offset),
            "offset": offset,
            "status": "待核对",
            "核对状态": "待核对",
            "pending": True,
            "abnormal": False,
            "已办": False,
            "processed": False,
            "分子": snapshot["分子"],
            "numerator": snapshot["numerator"],
            "分母": snapshot["分母"],
            "denominator": snapshot["denominator"],
            "有效用户数": snapshot["有效用户数"],
            "样本量": snapshot["样本量"],
            "实验组人数": snapshot["实验组人数"],
            "对照组人数": snapshot["对照组人数"],
            "重复排除数": snapshot["重复排除数"],
            "duplicate_excluded": snapshot["duplicate_excluded"],
            "duplicate_excluded_count": snapshot.get("duplicate_excluded_count", snapshot["重复排除数"]),
            "duplicates_excluded": snapshot.get("duplicates_excluded", snapshot["重复排除数"]),
            "duplicate_count": snapshot.get("duplicate_count", snapshot["重复排除数"]),
            "重复用户数": snapshot["重复用户数"],
            "名单内重复数": snapshot.get("名单内重复数", 0),
            "跨批次合并数": snapshot.get("跨批次合并数", 0),
            "剔除数": snapshot["剔除数"],
            "确认批次": ",".join(source_batches),
            "来源确认批次": source_batches,
            "confirmation_batches": source_batches,
            "分母来源": snapshot.get("分母来源", []),
            "denominator_sources": snapshot.get("denominator_sources", []),
            "发起回执": source_receipt,
            "source_receipt_id": source_receipt,
            "样本明细": copy.deepcopy(snapshot["members"]),
            "members": copy.deepcopy(snapshot["members"]),
            "领取明细": copy.deepcopy(snapshot["members"]),
            "claim_details": copy.deepcopy(snapshot["members"]),
            "用户归属": copy.deepcopy(snapshot["用户归属"]),
            "样本快照": copy.deepcopy(snapshot),
            "计算入口": "分组批次确认的有效用户",
            "calculation_source": "confirmed_assignment_receipts",
            "created_at": now_iso(),
            "登记时间": now_iso(),
        }
        for key in ("trigger_difference_id", "差异编号", "trigger_difference_code"):
            if options.get(key) not in (None, ""):
                entry[key] = options[key]

        ratio_rows.append(entry)
        result = {
            "ok": True,
            "message": (
                f"已创建核对批次 {check_code}：分子 {entry['分子']}，分母 {entry['分母']}，"
                f"重复排除 {entry['重复排除数']}"
            ),
            "entry": entry,
            "check": entry,
            "data": entry,
            "核对批次": check_code,
            "check_id": entry["id"],
        }
        result.update({k: v for k, v in snapshot.items() if k not in {"ok"}})

        if options.get("auto_run") or options.get("run_now") or options.get("execute"):
            checked = self.run_check(int(entry["id"]))
            result.update(checked)
            result["entry"] = store.find(self.MODULE, int(entry["id"]))
            result["check"] = result["entry"]
        return result

    def calculate_for_experiment(self, exp_code: str, **options: Any) -> dict[str, Any]:
        """计算入口：创建并立即执行一次核对。"""
        options["auto_run"] = True
        return self.create_check(exp_code, **options)

    calculate_ratio = calculate_for_experiment
    recompute = calculate_for_experiment
    recalculate = calculate_for_experiment
    calculate_sample_ratio = calculate_for_experiment

    def calculation_snapshot(self, exp_code: str) -> dict[str, Any]:
        return VariantAssignService().experiment_snapshot(exp_code)

    get_sample_counts = calculation_snapshot
    recalculate_ratio = calculate_for_experiment

    @staticmethod
    def _parse_ratio(value: Any) -> float | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        is_percent = "%" in text
        text = text.replace("%", "").strip()
        try:
            ratio = float(text)
        except (TypeError, ValueError):
            return None
        if is_percent:
            ratio /= 100.0
        elif ratio > 1:
            ratio /= 100.0
        return ratio

    # ---------- 执行核对 ----------

    def run_check(self, entry_id: int) -> dict[str, Any]:
        """执行比例核对：从确认归属重算；已办结批次保留原快照和原比例。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "核对批次不存在"}

        if entry.get("status") in {"已核对", "偏移超标"} and entry.get("样本快照"):
            return {
                "ok": True,
                "message": f"核对批次已办结，保留原比例 {entry.get('实际比例')}，不重复写入分子",
                "entry": entry,
                "preserved": True,
                "分子": entry.get("分子"),
                "分母": entry.get("分母"),
                "重复排除数": entry.get("重复排除数"),
            }
        if entry.get("status") != "待核对":
            return {"ok": False, "message": "仅待核对批次可执行"}

        exp_code = str(entry.get("实验编码", ""))
        snapshot = VariantAssignService().experiment_snapshot(exp_code)
        if int(snapshot.get("分母", 0)) <= 0:
            entry["status"] = "偏移超标"
            entry["abnormal"] = True
            entry["pending"] = True
            entry["message"] = "实验无有效分组，核对失败"
            return {"ok": False, "message": "实验无有效分组，核对失败", "entry": entry, **snapshot}

        actual = float(snapshot["实际比例"])
        planned = self._parse_ratio(entry.get("计划比例"))
        if planned is None:
            planned = 0.5
        offset = round(abs(actual - planned), 4)

        entry["实际比例"] = str(actual)
        entry["actual_ratio"] = actual
        entry["偏移量"] = str(offset)
        entry["offset"] = offset
        entry["分子"] = snapshot["分子"]
        entry["numerator"] = snapshot["numerator"]
        entry["分母"] = snapshot["分母"]
        entry["denominator"] = snapshot["denominator"]
        entry["有效用户数"] = snapshot["有效用户数"]
        entry["样本量"] = snapshot["样本量"]
        entry["实验组人数"] = snapshot["实验组人数"]
        entry["对照组人数"] = snapshot["对照组人数"]
        entry["重复排除数"] = snapshot["重复排除数"]
        entry["duplicate_excluded"] = snapshot["duplicate_excluded"]
        entry["duplicate_excluded_count"] = snapshot.get("duplicate_excluded_count", snapshot["重复排除数"])
        entry["duplicates_excluded"] = snapshot.get("duplicates_excluded", snapshot["重复排除数"])
        entry["duplicate_count"] = snapshot.get("duplicate_count", snapshot["重复排除数"])
        entry["重复用户数"] = snapshot["重复用户数"]
        entry["名单内重复数"] = snapshot.get("名单内重复数", 0)
        entry["跨批次合并数"] = snapshot.get("跨批次合并数", 0)
        entry["剔除数"] = snapshot["剔除数"]
        entry["来源确认批次"] = snapshot["来源确认批次"]
        entry["confirmation_batches"] = snapshot["confirmation_batches"]
        entry["分母来源"] = snapshot.get("分母来源", [])
        entry["denominator_sources"] = snapshot.get("denominator_sources", [])
        entry["确认批次"] = ",".join(snapshot["来源确认批次"])
        entry["样本明细"] = copy.deepcopy(snapshot["members"])
        entry["members"] = copy.deepcopy(snapshot["members"])
        entry["领取明细"] = copy.deepcopy(snapshot["members"])
        entry["claim_details"] = copy.deepcopy(snapshot["members"])
        entry["用户归属"] = copy.deepcopy(snapshot["用户归属"])
        entry["样本快照"] = copy.deepcopy(snapshot)
        entry["核对时间"] = now_iso()
        entry["checked"] = True
        entry["processed"] = True
        entry["已办结"] = True
        entry["已办"] = True
        entry["核对状态"] = "已核对"

        if offset > 0.05:
            entry["status"] = "偏移超标"
            entry["pending"] = True
            entry["abnormal"] = True
            return {
                "ok": True,
                "message": f"偏移 {offset} 超阈值 0.05，已告警；分子 {snapshot['分子']}，分母 {snapshot['分母']}",
                "entry": entry,
                **{k: v for k, v in snapshot.items() if k != "ok"},
            }
        entry["status"] = "已核对"
        entry["pending"] = False
        entry["abnormal"] = False
        return {
            "ok": True,
            "message": f"核对通过，偏移 {offset} 在阈值内；分子 {snapshot['分子']}，分母 {snapshot['分母']}",
            "entry": entry,
            **{k: v for k, v in snapshot.items() if k != "ok"},
        }

    def alert_offset(self, entry_id: int) -> dict[str, Any]:
        """把偏移超标批次登记为异常事件，进入质量处置流程。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "偏移超标":
            return {"ok": False, "message": "仅偏移超标批次可告警"}
        anomaly_rows = store.rows("anomaly_metric")
        anomaly_rows.append({
            "id": next_id(anomaly_rows),
            "异常编号": f"AN-SR-{entry_id}",
            "指标编码": str(entry.get("实验编码", "")),
            "基线区间": str(entry.get("计划比例", "")),
            "观测值": str(entry.get("实际比例", "")),
            "异常评分": "0.7",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        })
        entry["pending"] = True
        entry["告警时间"] = now_iso()
        return {"ok": True, "message": f"偏移告警已登记（偏移量 {entry.get('偏移量', '0')}）"}

    def batch_run_pending(self) -> dict[str, Any]:
        """批量执行待核对批次。"""
        pending = [r for r in store.rows(self.MODULE) if r.get("status") == "待核对"]
        passed, alarmed = 0, 0
        for row in pending:
            result = self.run_check(int(row.get("id", 0)))
            if result.get("ok"):
                if "超阈值" in str(result.get("message", "")):
                    alarmed += 1
                else:
                    passed += 1
        return {"ok": True, "message": f"批量核对完成：通过 {passed}，告警 {alarmed}"}

    # ---------- 剔除差异 -> 新核对 ----------

    def list_differences(self, exp_code: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(DIFF_TABLE))
        if exp_code:
            rows = [r for r in rows if r.get("实验编码") == exp_code]
        return list(reversed(rows))

    def get_difference(self, identifier: Any) -> dict[str, Any] | None:
        text = str(identifier or "").strip()
        rows = store.rows(DIFF_TABLE)
        if text.isdigit():
            found = next((r for r in rows if int(r.get("id", 0)) == int(text)), None)
            if found:
                return found
        return next((r for r in rows if str(r.get("差异编号")) == text or str(r.get("difference_id")) == text), None)

    def create_check_from_difference(self, identifier: Any, **options: Any) -> dict[str, Any]:
        """据剔除差异发起新核对；原核对批次和比例保持不变。"""
        diff = self.get_difference(identifier)
        if diff is None:
            return {"ok": False, "message": f"剔除差异 {identifier} 不存在"}

        existing_id = diff.get("new_check_id") or diff.get("新核对ID")
        if existing_id:
            existing = store.find(self.MODULE, int(existing_id)) if str(existing_id).isdigit() else None
            if existing is not None:
                return {
                    "ok": True,
                    "message": "该差异已发起过新核对，返回已有批次",
                    "check": existing,
                    "entry": existing,
                    "difference": diff,
                    "idempotent_replay": True,
                }

        original_check = None
        original_batch = str(diff.get("原核对批次") or "")
        if original_batch:
            original_check = next(
                (r for r in store.rows(self.MODULE) if r.get("核对批次") == original_batch),
                None,
            )
        planned_ratio = (original_check or {}).get("计划比例") or options.get("planned_ratio")

        result = self.create_check(
            str(diff.get("实验编码")),
            planned_ratio=planned_ratio,
            trigger_difference_id=diff.get("id"),
            trigger_difference_code=diff.get("差异编号"),
            **{k: v for k, v in options.items() if k not in {"planned_ratio"}},
        )
        if not result.get("ok"):
            return result

        check = result["entry"]
        diff.update({
            "status": "已发起核对",
            "state": "check_created",
            "办理状态": "已发起",
            "pending": False,
            "new_check_id": check.get("id"),
            "新核对ID": check.get("id"),
            "new_check_batch": check.get("核对批次"),
            "新核对批次": check.get("核对批次"),
            "发起时间": now_iso(),
        })
        result["difference"] = diff
        result["message"] = f"已据差异 {diff.get('差异编号')} 发起新核对 {check.get('核对批次')}，原比例 {diff.get('原比例')} 保留"
        return result

    initiate_check_from_difference = create_check_from_difference
    new_check_from_difference = create_check_from_difference

    # ---------- 下游兼容读取 ----------

    def consume_from_variant_assign(self, exp_code: str) -> dict[str, Any]:
        """读取分组分配器去重后的分布统计，作为核对输入。"""
        snapshot = VariantAssignService().experiment_snapshot(exp_code)
        assignment_total = sum(
            1 for r in store.rows("variant_assign") if r.get("实验编码") == exp_code
        )
        if snapshot.get("分母", 0) <= 0:
            return {"ok": False, "message": "实验无有效分组数据"}
        return {
            "ok": True,
            "总分配数": assignment_total,
            "名单明细数": snapshot["raw_detail_count"],
            "raw_detail_count": snapshot["raw_detail_count"],
            "有效分配数": snapshot["分母"],
            "分子": snapshot["分子"],
            "分母": snapshot["分母"],
            "重复排除数": snapshot["重复排除数"],
            "实验组占比": snapshot["实际比例"],
            "来源确认批次": snapshot["来源确认批次"],
        }

    def consume_from_exp_design(self, exp_code: str) -> dict[str, Any]:
        """读取实验设计层流量比例作为计划比例。"""
        exp = next((r for r in store.rows("exp_design") if r.get("实验编码") == exp_code), None)
        if exp is None:
            return {"ok": False, "message": "实验方案不存在"}
        planned = self._parse_ratio(exp.get("流量比例", "0.5"))
        if planned is None:
            planned = 0.5
        return {"ok": True, "计划比例": str(planned), "分组策略": exp.get("分组策略", "")}

    def export_offset_trend(self, exp_code: str) -> list[dict[str, Any]]:
        """导出实验偏移趋势，供报告生成器写入数据规模说明。"""
        return [
            {
                "核对批次": r.get("核对批次", ""),
                "计划比例": r.get("计划比例", ""),
                "实际比例": r.get("实际比例", ""),
                "偏移量": r.get("偏移量", ""),
                "分子": r.get("分子", ""),
                "分母": r.get("分母", ""),
                "重复排除数": r.get("重复排除数", ""),
            }
            for r in store.rows(self.MODULE) if r.get("实验编码") == exp_code
        ]


class SampleRatioDifferenceService(BaseModuleService):
    """把剔除差异也挂到通用资源路由下，便于列表/动作接口访问。"""

    MODULE = DIFF_TABLE
    ENTITY = "剔除差异"
    LIST_FIELDS = ["差异编号", "实验编码", "用户标识", "确认批次", "实际组别", "原比例", "status"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["待发起核对", "已发起核对"]
    ACTION_RULES = {"发起新核对": "已发起核对"}
    NEGATIVE_ACTIONS: list[str] = []

    def run_action(
        self,
        entry_id: int,
        action: str,
        payload: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action in {"发起新核对", "发起核对", "据差异发起核对"}:
            result = SampleRatioService().create_check_from_difference(entry_id)
            entry = store.find(DIFF_TABLE, entry_id)
            return (entry if result.get("ok") else None), str(result.get("message", ""))
        return super().run_action(entry_id, action, payload)
