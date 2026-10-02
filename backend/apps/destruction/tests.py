"""
物资销毁四阶段流程测试
"""
import datetime
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import (
    Approval, Category, Goods, GoodsHold, StockOut, Unit, Variety,
)

from .models import (
    DestructionItem, DestructionPlan, DestructionStageSnapshot,
    ItemStatus, PlanStatus, SnapshotStage,
)
from .services import (
    DestructionError, check_eligibility, confirm_execution, correct_metadata,
    create_plan, decide_plan, submit_review,
)

PAST_DATE = date(2026, 1, 1)
FUTURE_DATE = date(2030, 12, 31)


class DestructionFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("dest-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(name="证物", unit=self.unit, created_by=self.user)
        self.variety = Variety.objects.create(name="存储介质", category=self.category, created_by=self.user)

    def make_goods(self, code, qty="10", expire=PAST_DATE):
        return Goods.objects.create(
            variety=self.variety, name=f"物资-{code}", code=code,
            quantity=Decimal(qty), retention_expire_date=expire,
        )

    def freeze(self, goods, reason="涉案冻结"):
        return GoodsHold.objects.create(
            goods=goods, hold_type="freeze", reason=reason, created_by=self.user)

    def investigate(self, goods):
        return GoodsHold.objects.create(
            goods=goods, hold_type="investigation", reason="未结调查", created_by=self.user)

    def full_flow_until_approved(self, goods_list):
        """建计划 → 复核 → 全项批准，返回 plan"""
        plan, _, _ = create_plan(
            operator=self.user, title="到期销毁",
            goods_ids=[g.id for g in goods_list])
        submit_review(plan_id=plan.id, operator=self.user)
        decide_plan(plan_id=plan.id, operator=self.user, approved=True)
        plan.refresh_from_db()
        return plan


# ==================== 物资前置状态检查 ====================

class EligibilityCheckTest(DestructionFixture):
    def test_expired_goods_eligible(self):
        goods = self.make_goods("G-001")
        passed, reasons = check_eligibility(goods)
        self.assertTrue(passed, reasons)

    def test_frozen_blocks(self):
        goods = self.make_goods("G-002")
        self.freeze(goods)
        passed, reasons = check_eligibility(goods)
        self.assertFalse(passed)
        self.assertIn("物资处于冻结中", reasons)

    def test_open_investigation_blocks(self):
        goods = self.make_goods("G-003")
        self.investigate(goods)
        passed, reasons = check_eligibility(goods)
        self.assertFalse(passed)
        self.assertIn("物资存在未结调查", reasons)

    def test_open_stock_out_blocks(self):
        goods = self.make_goods("G-004")
        StockOut.objects.create(goods=goods, operator=self.user,
                                receiver="张三", quantity=Decimal("1"), status="pending")
        passed, reasons = check_eligibility(goods)
        self.assertFalse(passed)
        self.assertIn("物资存在未结领用", reasons)
        # 已完成的领用不拦截
        StockOut.objects.filter(goods=goods).update(status="completed")
        self.assertTrue(check_eligibility(goods)[0])

    def test_retention_not_expired_blocks(self):
        goods = self.make_goods("G-005", expire=FUTURE_DATE)
        passed, reasons = check_eligibility(goods)
        self.assertFalse(passed)
        self.assertIn("保管期限尚未届满", reasons)

    def test_zero_quantity_blocks(self):
        goods = self.make_goods("G-006", qty="0")
        passed, _ = check_eligibility(goods)
        self.assertFalse(passed)


# ==================== 阶段一：销毁计划 ====================

class PlanningStageTest(DestructionFixture):
    def test_create_plan_saves_snapshot(self):
        goods = self.make_goods("G-101")
        plan, snapshot, rejected = create_plan(
            operator=self.user, title="2026Q1 销毁", goods_ids=[goods.id])

        self.assertEqual(plan.status, PlanStatus.PLANNED)
        self.assertTrue(plan.plan_no.startswith("DS"))
        self.assertEqual(plan.item_count, 1)
        self.assertEqual(snapshot.stage, SnapshotStage.PLANNING)
        self.assertEqual(snapshot.summary["total"], 1)
        self.assertTrue(snapshot.checksum)
        item = plan.items.get()
        self.assertEqual(item.status, ItemStatus.INCLUDED)
        self.assertEqual(item.quantity_at_plan, Decimal("10"))
        self.assertEqual(rejected, [])

    def test_frozen_goods_listed_with_warning_but_included(self):
        """冻结物资允许列入（记录风险提示），拦截发生在复核/执行前"""
        goods = self.make_goods("G-102")
        self.freeze(goods)
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        item = plan.items.get()
        self.assertEqual(item.status, ItemStatus.INCLUDED)
        self.assertIn("物资处于冻结中", item.plan_warnings)

    def test_destroyed_goods_cannot_be_listed(self):
        goods = self.make_goods("G-103")
        plan = self.full_flow_until_approved([goods])
        confirm_execution(plan_id=plan.id, operator=self.user)
        with self.assertRaises(DestructionError):
            create_plan(operator=self.user, title="重复销毁", goods_ids=[goods.id])

    def test_goods_in_active_plan_cannot_relist(self):
        goods = self.make_goods("G-104")
        create_plan(operator=self.user, title="计划一", goods_ids=[goods.id])
        with self.assertRaises(DestructionError):
            create_plan(operator=self.user, title="计划二", goods_ids=[goods.id])

    def test_empty_list_rejected(self):
        with self.assertRaises(DestructionError):
            create_plan(operator=self.user, title="空计划", goods_ids=[])

    def test_duplicate_goods_ids_in_one_request(self):
        goods = self.make_goods("G-105")
        plan, _, _ = create_plan(
            operator=self.user, title="去重", goods_ids=[goods.id, goods.id])
        self.assertEqual(plan.items.count(), 1)


# ==================== 阶段二：资格复核 ====================

class ReviewStageTest(DestructionFixture):
    def test_eligible_items_pass_review(self):
        goods = self.make_goods("G-201")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        _, snapshot, excluded = submit_review(plan_id=plan.id, operator=self.user)

        plan.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.REVIEWED)
        self.assertEqual(plan.items.get().status, ItemStatus.ELIGIBLE)
        self.assertEqual(excluded, [])
        self.assertEqual(snapshot.stage, SnapshotStage.REVIEW)

    def test_frozen_item_excluded_at_review(self):
        good_a = self.make_goods("G-202")
        good_b = self.make_goods("G-203")
        self.freeze(good_b)
        plan, _, _ = create_plan(
            operator=self.user, title="计划", goods_ids=[good_a.id, good_b.id])
        _, _, excluded = submit_review(plan_id=plan.id, operator=self.user)

        statuses = {it.goods_code: it.status for it in plan.items.all()}
        self.assertEqual(statuses["G-202"], ItemStatus.ELIGIBLE)
        self.assertEqual(statuses["G-203"], ItemStatus.EXCLUDED)
        blocked_item = plan.items.get(goods=good_b)
        self.assertIn("物资处于冻结中", blocked_item.block_reasons)
        self.assertEqual(len(excluded), 1)

    def test_cannot_review_twice(self):
        goods = self.make_goods("G-204")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        submit_review(plan_id=plan.id, operator=self.user)
        with self.assertRaises(DestructionError):
            submit_review(plan_id=plan.id, operator=self.user)

    def test_review_failure_isolated_per_item(self):
        """单项复核抛异常不应让其他项含糊：异常项标记剔除，其他项照常合格"""
        good_a = self.make_goods("G-205")
        good_b = self.make_goods("G-206")
        plan, _, _ = create_plan(
            operator=self.user, title="计划", goods_ids=[good_a.id, good_b.id])

        real_check = check_eligibility

        def flaky(goods):
            if goods.code == "G-206":
                raise RuntimeError("boom")
            return real_check(goods)

        with patch("apps.destruction.services.check_eligibility", side_effect=flaky):
            _, _, excluded = submit_review(plan_id=plan.id, operator=self.user)

        statuses = {it.goods_code: it.status for it in plan.items.all()}
        self.assertEqual(statuses["G-205"], ItemStatus.ELIGIBLE)
        self.assertEqual(statuses["G-206"], ItemStatus.EXCLUDED)
        self.assertIn("复核处理异常", excluded[0]["reasons"][0])


# ==================== 阶段三：批准 ====================

class ApprovalStageTest(DestructionFixture):
    def test_full_approval(self):
        goods = self.make_goods("G-301")
        plan = self.full_flow_until_approved([goods])
        self.assertEqual(plan.status, PlanStatus.APPROVED)
        self.assertEqual(plan.items.get().status, ItemStatus.APPROVED)

    def test_partial_item_rejection(self):
        good_a, good_b = self.make_goods("G-302"), self.make_goods("G-303")
        plan, _, _ = create_plan(
            operator=self.user, title="计划", goods_ids=[good_a.id, good_b.id])
        submit_review(plan_id=plan.id, operator=self.user)
        item_b = plan.items.get(goods=good_b)
        decide_plan(plan_id=plan.id, operator=self.user, approved=True,
                    item_decisions={str(item_b.id): {"approved": False, "reason": "存疑"}})

        self.assertEqual(plan.items.get(goods=good_a).status, ItemStatus.APPROVED)
        rejected_item = plan.items.get(goods=good_b)
        self.assertEqual(rejected_item.status, ItemStatus.REJECTED)
        self.assertEqual(rejected_item.block_reasons, ["存疑"])

    def test_approve_none_rejected_requires_reject_action(self):
        goods = self.make_goods("G-304")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        submit_review(plan_id=plan.id, operator=self.user)
        item = plan.items.get()
        with self.assertRaises(DestructionError):
            decide_plan(plan_id=plan.id, operator=self.user, approved=True,
                        item_decisions={str(item.id): {"approved": False}})

    def test_batch_reject_abandons_plan(self):
        goods = self.make_goods("G-305")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        submit_review(plan_id=plan.id, operator=self.user)
        plan, snapshot, _ = decide_plan(
            plan_id=plan.id, operator=self.user, approved=False, remark="整批不批")
        self.assertEqual(plan.status, PlanStatus.ABANDONED)
        self.assertTrue(plan.is_terminal)
        self.assertEqual(plan.items.get().status, ItemStatus.REJECTED)
        self.assertEqual(snapshot.stage, SnapshotStage.APPROVAL)

    def test_excluded_items_skipped_at_approval(self):
        good_a, good_b = self.make_goods("G-306"), self.make_goods("G-307")
        self.freeze(good_b)
        plan, _, _ = create_plan(
            operator=self.user, title="计划", goods_ids=[good_a.id, good_b.id])
        submit_review(plan_id=plan.id, operator=self.user)
        decide_plan(plan_id=plan.id, operator=self.user, approved=True)
        self.assertEqual(plan.items.get(goods=good_a).status, ItemStatus.APPROVED)
        self.assertEqual(plan.items.get(goods=good_b).status, ItemStatus.EXCLUDED)


# ==================== 阶段四：执行确认 ====================

class ExecutionStageTest(DestructionFixture):
    def test_execute_sets_irreversible_terminal_state(self):
        goods = self.make_goods("G-401")
        plan = self.full_flow_until_approved([goods])
        _, snapshot, blocked, destroyed = confirm_execution(
            plan_id=plan.id, operator=self.user)

        plan.refresh_from_db()
        goods.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.EXECUTED)
        self.assertTrue(plan.is_terminal)
        self.assertEqual(plan.items.get().status, ItemStatus.DESTROYED)
        self.assertEqual(goods.quantity, Decimal("0"))
        self.assertEqual(goods.lifecycle_status, "destroyed")
        self.assertTrue(goods.is_destroyed)
        self.assertFalse(goods.is_active)
        self.assertEqual(blocked, [])
        self.assertEqual(len(destroyed), 1)
        self.assertEqual(snapshot.stage, SnapshotStage.EXECUTION)

    def test_freeze_after_approval_blocks_at_execution_others_proceed(self):
        """执行前再次检查：批准后才被冻结的物资必须拦截，且不连累其他项"""
        good_a, good_b, good_c = (self.make_goods("G-402"),
                                  self.make_goods("G-403"),
                                  self.make_goods("G-404"))
        plan = self.full_flow_until_approved([good_a, good_b, good_c])
        self.freeze(good_b)
        self.investigate(good_c)

        _, _, blocked, destroyed = confirm_execution(
            plan_id=plan.id, operator=self.user)

        plan.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.EXECUTED)
        statuses = {it.goods_code: it.status for it in plan.items.all()}
        self.assertEqual(statuses["G-402"], ItemStatus.DESTROYED)
        self.assertEqual(statuses["G-403"], ItemStatus.BLOCKED)
        self.assertEqual(statuses["G-404"], ItemStatus.BLOCKED)

        good_a.refresh_from_db(); good_b.refresh_from_db(); good_c.refresh_from_db()
        self.assertEqual(good_a.quantity, Decimal("0"))
        self.assertTrue(good_a.is_destroyed)
        # 被拦截物资库存原样保留、仍在库
        self.assertEqual(good_b.quantity, Decimal("10"))
        self.assertFalse(good_b.is_destroyed)
        self.assertEqual(good_c.quantity, Decimal("10"))

        self.assertEqual(len(blocked), 2)
        self.assertEqual(len(destroyed), 1)
        # 执行快照中按拦截原因分类
        exec_snapshot = plan.snapshots.get(stage=SnapshotStage.EXECUTION)
        self.assertEqual(exec_snapshot.summary["checks"]["frozen"],
                         [plan.items.get(goods=good_b).id])
        self.assertIn(plan.items.get(goods=good_c).id,
                      exec_snapshot.summary["checks"]["investigation"])

    def test_all_blocked_keeps_plan_approved_no_destruction(self):
        """全部拦截时计划停留 approved，不产生任何销毁，不算含糊终态"""
        goods = self.make_goods("G-405")
        plan = self.full_flow_until_approved([goods])
        self.freeze(goods)
        _, _, blocked, destroyed = confirm_execution(
            plan_id=plan.id, operator=self.user)

        plan.refresh_from_db()
        goods.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.APPROVED)
        self.assertEqual(plan.items.get().status, ItemStatus.BLOCKED)
        self.assertEqual(goods.quantity, Decimal("10"))
        self.assertFalse(goods.is_destroyed)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(destroyed, [])
        # 即使全部拦截也保存执行阶段快照，留痕拦截事实
        self.assertTrue(plan.snapshots.filter(stage=SnapshotStage.EXECUTION).exists())

    def test_execute_failure_isolated_per_item(self):
        """单项执行异常只回滚该项：其他项正常销毁，异常项 blocked"""
        good_a, good_b = self.make_goods("G-406"), self.make_goods("G-407")
        plan = self.full_flow_until_approved([good_a, good_b])

        real_check = check_eligibility

        def flaky(goods):
            if goods.code == "G-407":
                raise RuntimeError("db glitch")
            return real_check(goods)

        with patch("apps.destruction.services.check_eligibility", side_effect=flaky):
            _, _, blocked, destroyed = confirm_execution(
                plan_id=plan.id, operator=self.user)

        statuses = {it.goods_code: it.status for it in plan.items.all()}
        self.assertEqual(statuses["G-406"], ItemStatus.DESTROYED)
        self.assertEqual(statuses["G-407"], ItemStatus.BLOCKED)
        self.assertEqual(len(destroyed), 1)
        self.assertEqual(blocked[0]["reasons"][0], "执行处理异常：db glitch")
        good_b.refresh_from_db()
        self.assertEqual(good_b.quantity, Decimal("10"))

    def test_blocked_item_recheckable_after_hold_released(self):
        """全部拦截后计划停留 approved；解除冻结再次执行确认应成功"""
        goods = self.make_goods("G-410")
        plan = self.full_flow_until_approved([goods])
        hold = self.freeze(goods)
        confirm_execution(plan_id=plan.id, operator=self.user)
        self.assertEqual(plan.items.get().status, ItemStatus.BLOCKED)

        hold.release(self.user, reason="冻结解除")
        _, _, blocked, destroyed = confirm_execution(
            plan_id=plan.id, operator=self.user)
        plan.refresh_from_db()
        goods.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.EXECUTED)
        self.assertEqual(plan.items.get().status, ItemStatus.DESTROYED)
        self.assertTrue(goods.is_destroyed)
        self.assertEqual(blocked, [])
        self.assertEqual(len(destroyed), 1)

    def test_illegal_transitions_rejected(self):
        goods = self.make_goods("G-408")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        # 未复核不可批准
        with self.assertRaises(DestructionError):
            decide_plan(plan_id=plan.id, operator=self.user, approved=True)
        # 未批准不可执行
        with self.assertRaises(DestructionError):
            confirm_execution(plan_id=plan.id, operator=self.user)
        # 执行后不可再执行
        plan2 = self.full_flow_until_approved([self.make_goods("G-409")])
        confirm_execution(plan_id=plan2.id, operator=self.user)
        with self.assertRaises(DestructionError):
            confirm_execution(plan_id=plan2.id, operator=self.user)


# ==================== 四阶段快照完整性 ====================

class SnapshotTest(DestructionFixture):
    def test_four_stage_snapshots_persisted(self):
        goods = self.make_goods("G-501")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        submit_review(plan_id=plan.id, operator=self.user)
        decide_plan(plan_id=plan.id, operator=self.user, approved=True)
        confirm_execution(plan_id=plan.id, operator=self.user)

        stages = list(plan.snapshots.values_list("stage", flat=True))
        self.assertEqual(stages, ["planning", "review", "approval", "execution"])
        for snap in plan.snapshots.all():
            self.assertEqual(snap.summary["plan_no"], plan.plan_no)
            self.assertTrue(snap.checksum)
            self.assertEqual(snap.summary["by_status"].get(
                {"planning": "included", "review": "eligible",
                 "approval": "approved", "execution": "destroyed"}[snap.stage], 0), 1)

    def test_snapshots_are_append_only_records(self):
        goods = self.make_goods("G-502")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        submit_review(plan_id=plan.id, operator=self.user)
        self.assertEqual(
            DestructionStageSnapshot.objects.filter(plan=plan).count(), 2)


# ==================== 销毁后元数据更正 ====================

class CorrectionTest(DestructionFixture):
    def _executed_item(self, code="G-601"):
        goods = self.make_goods(code)
        plan = self.full_flow_until_approved([goods])
        confirm_execution(plan_id=plan.id, operator=self.user)
        return plan.items.get(), goods

    def test_correct_metadata_does_not_restore_stock(self):
        item, goods = self._executed_item()
        _, records = correct_metadata(
            item_id=item.id, operator=self.user,
            changes={"location": "档案库A架", "remark": "编号录入有误"},
            reason="执行后发现存放位置录入错误")

        goods.refresh_from_db()
        self.assertEqual(goods.location, "档案库A架")
        self.assertEqual(goods.quantity, Decimal("0"))           # 库存不恢复
        self.assertEqual(goods.lifecycle_status, "destroyed")    # 终态不变
        self.assertTrue(goods.is_destroyed)
        self.assertEqual(len(records), 2)
        self.assertEqual(item.corrections.count(), 2)

    def test_correct_code_keeps_item_snapshot_value(self):
        item, goods = self._executed_item("G-602")
        _, records = correct_metadata(
            item_id=item.id, operator=self.user,
            changes={"code": "G-602-FIX"}, reason="编码录入错误")
        item.refresh_from_db()
        goods.refresh_from_db()
        # 物资主数据编码已更正
        self.assertEqual(goods.code, "G-602-FIX")
        # 但条目上的列入时快照编码保持不变，前后值由更正记录留痕
        self.assertEqual(item.goods_code, "G-602")
        self.assertEqual(records[0].old_value, "G-602")
        self.assertEqual(records[0].new_value, "G-602-FIX")

    def test_correction_rejects_non_whitelisted_fields(self):
        item, goods = self._executed_item()
        with self.assertRaises(DestructionError):
            correct_metadata(item_id=item.id, operator=self.user,
                             changes={"quantity": "100"}, reason="尝试恢复库存")
        with self.assertRaises(DestructionError):
            correct_metadata(item_id=item.id, operator=self.user,
                             changes={"lifecycle_status": "in_custody"}, reason="尝试撤销销毁")
        goods.refresh_from_db()
        self.assertEqual(goods.quantity, Decimal("0"))
        self.assertEqual(goods.lifecycle_status, "destroyed")
        self.assertEqual(item.corrections.count(), 0)

    def test_correction_requires_reason(self):
        item, _ = self._executed_item("G-603")
        with self.assertRaises(DestructionError):
            correct_metadata(item_id=item.id, operator=self.user,
                             changes={"location": "X"}, reason="")

    def test_correction_only_for_destroyed_item(self):
        goods = self.make_goods("G-604")
        plan, _, _ = create_plan(operator=self.user, title="计划", goods_ids=[goods.id])
        with self.assertRaises(DestructionError):
            correct_metadata(item_id=plan.items.get().id, operator=self.user,
                             changes={"location": "X"}, reason="未销毁先改")

    def test_duplicate_code_rejected(self):
        item, _ = self._executed_item("G-605")
        self.make_goods("G-606")
        with self.assertRaises(DestructionError):
            correct_metadata(item_id=item.id, operator=self.user,
                             changes={"code": "G-606"}, reason="重码")

    def test_noop_change_rejected(self):
        item, _ = self._executed_item("G-607")
        with self.assertRaises(DestructionError):
            correct_metadata(item_id=item.id, operator=self.user,
                             changes={"location": ""}, reason="无变化")


# ==================== API 端到端 ====================

class DestructionAPITest(DestructionFixture):
    def test_full_flow_api(self):
        goods = self.make_goods("G-701")
        created = self.client.post("/api/destruction/plans/", {
            "title": "API 销毁计划", "goods_ids": [goods.id],
        }, format="json")
        self.assertEqual(created.status_code, 200, created.content)
        plan_id = created.json()["data"]["plan"]["id"]
        self.assertTrue(created.json()["data"]["snapshot_id"])

        reviewed = self.client.post(f"/api/destruction/plans/{plan_id}/review/",
                                    {"remark": "复核无误"}, format="json")
        self.assertEqual(reviewed.status_code, 200)

        approved = self.client.post(f"/api/destruction/plans/{plan_id}/approval/",
                                    {"approved": True}, format="json")
        self.assertEqual(approved.status_code, 200)

        executed = self.client.post(f"/api/destruction/plans/{plan_id}/execute/",
                                    {"remark": "已现场销毁"}, format="json")
        self.assertEqual(executed.status_code, 200)
        self.assertEqual(executed.json()["data"]["plan"]["status"], "executed")
        self.assertEqual(len(executed.json()["data"]["destroyed_item_ids"]), 1)

        # 每阶段快照可查询
        snaps = self.client.get(f"/api/destruction/plans/{plan_id}/snapshots/").json()
        self.assertEqual(snaps["data"]["total"], 4)

        # 详情含条目、快照、更正
        detail = self.client.get(f"/api/destruction/plans/{plan_id}/").json()
        self.assertEqual(detail["data"]["items"][0]["status"], "destroyed")
        self.assertEqual(len(detail["data"]["snapshots"]), 4)

    def test_frozen_blocked_via_api(self):
        good_a, good_b = self.make_goods("G-702"), self.make_goods("G-703")
        plan_id = self.client.post("/api/destruction/plans/", {
            "title": "拦截测试", "goods_ids": [good_a.id, good_b.id],
        }, format="json").json()["data"]["plan"]["id"]
        self.client.post(f"/api/destruction/plans/{plan_id}/review/", {}, format="json")
        self.client.post(f"/api/destruction/plans/{plan_id}/approval/",
                         {"approved": True}, format="json")
        # 批准后冻结 good_b
        self.freeze(good_b)
        resp = self.client.post(f"/api/destruction/plans/{plan_id}/execute/",
                                {}, format="json").json()
        self.assertEqual(len(resp["data"]["destroyed_item_ids"]), 1)
        self.assertEqual(resp["data"]["blocked"][0]["code"], "G-703")
        self.assertIn("冻结", resp["data"]["blocked"][0]["reasons"][0])

    def test_precheck_api(self):
        goods = self.make_goods("G-704")
        self.freeze(goods)
        resp = self.client.post("/api/destruction/eligibility-precheck/",
                                {"goods_ids": [goods.id]}, format="json").json()
        row = resp["data"]["results"][0]
        self.assertFalse(row["eligible"])
        self.assertTrue(row["is_frozen"])

    def test_correction_api_allows_whitelist_and_denies_stock(self):
        goods = self.make_goods("G-705")
        plan_id = self.client.post("/api/destruction/plans/", {
            "title": "更正测试", "goods_ids": [goods.id],
        }, format="json").json()["data"]["plan"]["id"]
        self.client.post(f"/api/destruction/plans/{plan_id}/review/", {}, format="json")
        self.client.post(f"/api/destruction/plans/{plan_id}/approval/",
                         {"approved": True}, format="json")
        self.client.post(f"/api/destruction/plans/{plan_id}/execute/", {}, format="json")
        item_id = DestructionItem.objects.get(goods=goods).id

        denied = self.client.post(
            f"/api/destruction/items/{item_id}/corrections/",
            {"changes": {"quantity": "99"}, "reason": "试图恢复"}, format="json")
        self.assertEqual(denied.status_code, 400)

        ok = self.client.post(
            f"/api/destruction/items/{item_id}/corrections/",
            {"changes": {"name": "物资-G-705-更正"}, "reason": "名称录入错误"},
            format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        goods.refresh_from_db()
        self.assertEqual(goods.quantity, Decimal("0"))
        self.assertEqual(goods.lifecycle_status, "destroyed")

    def test_correction_requires_reason_api(self):
        goods = self.make_goods("G-706")
        plan_id = self.client.post("/api/destruction/plans/", {
            "title": "计划", "goods_ids": [goods.id]}, format="json"
        ).json()["data"]["plan"]["id"]
        self.client.post(f"/api/destruction/plans/{plan_id}/review/", {}, format="json")
        self.client.post(f"/api/destruction/plans/{plan_id}/approval/",
                         {"approved": True}, format="json")
        self.client.post(f"/api/destruction/plans/{plan_id}/execute/", {}, format="json")
        item_id = DestructionItem.objects.get(goods=goods).id
        resp = self.client.post(
            f"/api/destruction/items/{item_id}/corrections/",
            {"changes": {"location": "X"}, "reason": ""}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_plan_list_and_status_filter(self):
        self.make_goods("G-707")
        resp = self.client.post("/api/destruction/plans/", {
            "title": "列表测试", "goods_ids": [Goods.objects.get(code="G-707").id],
        }, format="json")
        self.assertEqual(resp.status_code, 200)
        listed = self.client.get("/api/destruction/plans/?status=planned").json()
        self.assertEqual(listed["data"]["total"], 1)
        self.assertEqual(listed["data"]["list"][0]["item_count"], 1)

    def test_requires_authentication(self):
        resp = APIClient().get("/api/destruction/plans/")
        self.assertEqual(resp.status_code, 401)


class GoodsHoldApiTest(DestructionFixture):
    def test_freeze_and_release_flow(self):
        goods = self.make_goods("G-801")
        created = self.client.post("/api/goods-holds/", {
            "goods": goods.id, "hold_type": "freeze", "reason": "冻结令001",
        }, format="json")
        self.assertEqual(created.status_code, 200, created.content)
        hold_id = created.json()["data"]["id"]

        goods.refresh_from_db()
        self.assertTrue(goods.is_frozen)

        # 重复冻结被拒
        dup = self.client.post("/api/goods-holds/", {
            "goods": goods.id, "hold_type": "freeze"}, format="json")
        self.assertEqual(dup.status_code, 400)

        released = self.client.post(f"/api/goods-holds/{hold_id}/release/",
                                    {"reason": "冻结解除"}, format="json")
        self.assertEqual(released.status_code, 200)
        goods.refresh_from_db()
        self.assertFalse(goods.is_frozen)
        self.assertTrue(goods.has_open_investigation is False)

    def test_investigation_hold(self):
        goods = self.make_goods("G-802")
        self.client.post("/api/goods-holds/", {
            "goods": goods.id, "hold_type": "investigation", "reason": "调查中",
        }, format="json")
        goods.refresh_from_db()
        self.assertTrue(goods.has_open_investigation)
        self.assertFalse(goods.is_frozen)

    def test_goods_list_exposes_flags(self):
        goods = self.make_goods("G-803")
        self.freeze(goods)
        row = self.client.get("/api/goods/?keyword=G-803").json()["data"]["list"][0]
        self.assertTrue(row["is_frozen"])
        self.assertTrue(row["is_retention_expired"])
        self.assertFalse(row["is_destroyed"])

    def test_destroyed_goods_rejects_normal_edit(self):
        goods = self.make_goods("G-804")
        plan = self.full_flow_until_approved([goods])
        confirm_execution(plan_id=plan.id, operator=self.user)
        resp = self.client.put(f"/api/goods/{goods.id}/", {
            "name": "新名", "code": "G-804", "variety": self.variety.id,
            "quantity": "5",
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        goods.refresh_from_db()
        self.assertEqual(goods.quantity, Decimal("0"))
