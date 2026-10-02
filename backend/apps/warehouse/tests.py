import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from .models import (
    Approval, Category, DestructionCorrection, DestructionPlan, DestructionPlanItem,
    DestructionStageSnapshot, Goods, Investigation, StockIn, StockOut, Unit, Variety, Warning,
)


class WarehouseFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("warehouse-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(name="受控器材", unit=self.unit, created_by=self.user)
        self.variety = Variety.objects.create(name="记录终端", category=self.category, created_by=self.user)
        self.goods = Goods.objects.create(
            variety=self.variety,
            name="执法记录终端",
            code="DEV-001",
            quantity=Decimal("12"),
            warning_threshold=Decimal("5"),
        )


class WarehouseModelTest(WarehouseFixture):
    def test_relationship_flags(self):
        self.assertTrue(self.unit.is_linked)
        self.assertTrue(self.category.is_linked)
        self.assertTrue(self.variety.is_in_stock)
        self.assertFalse(self.goods.is_warning)

    def test_unique_unit_name(self):
        with self.assertRaises(IntegrityError):
            Unit.objects.create(name="件", created_by=self.user)

    def test_stock_records_and_approval(self):
        inbound = StockIn.objects.create(goods=self.goods, operator=self.user, quantity=Decimal("3"))
        outbound = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="保管员", quantity=Decimal("2")
        )
        approval = Approval.objects.create(stock_out=outbound, approver=self.user)
        self.assertEqual(inbound.goods_id, self.goods.id)
        self.assertEqual(approval.status, "pending")

    def test_warning_record(self):
        warning = Warning.objects.create(goods=self.goods, type="low_stock", message="库存不足")
        self.assertFalse(warning.is_read)
        self.assertIn("执法记录终端", str(warning))


class WarehouseAPITest(WarehouseFixture):
    def test_list_units(self):
        response = self.client.get("/api/units/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)

    def test_create_unit_and_reject_duplicate(self):
        created = self.client.post("/api/units/", {"name": "箱"}, format="json")
        duplicate = self.client.post("/api/units/", {"name": "箱"}, format="json")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_update_linked_unit(self):
        response = self.client.put(f"/api/units/{self.unit.id}/", {"name": "台"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.name, "台")

    def test_refuse_delete_linked_unit(self):
        response = self.client.delete(f"/api/units/{self.unit.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Unit.objects.filter(pk=self.unit.id).exists())

    def test_create_category_validates_unit(self):
        ok = self.client.post("/api/categories/", {"name": "封存介质", "unit": self.unit.id}, format="json")
        bad = self.client.post("/api/categories/", {"name": "无效分类", "unit": 99999}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 400)

    def test_create_variety_and_duplicate_boundary(self):
        ok = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        duplicate = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_requires_authentication(self):
        anonymous = APIClient().get("/api/units/")
        self.assertEqual(anonymous.status_code, 401)


class DestructionWorkflowTest(WarehouseFixture):
    """销毁流程测试：计划 -> 复核 -> 批准 -> 执行 -> 更正"""

    def setUp(self):
        super().setUp()
        self.goods.custody_until = date.today() - timedelta(days=1)
        self.goods.save()
        self.goods2 = Goods.objects.create(
            variety=self.variety,
            name="封存摄像机",
            code="DEV-002",
            quantity=Decimal("5"),
            custody_until=date.today() - timedelta(days=1),
        )

    def _create_plan(self, items):
        return self.client.post("/api/destruction-plans/", {
            "title": "到期物资销毁计划",
            "items": items,
        }, format="json")

    def _default_items(self):
        return [
            {"goods": self.goods.id, "quantity": "3", "reason": "保管期限届满", "method": "粉碎"},
            {"goods": self.goods2.id, "quantity": "2", "reason": "保管期限届满"},
        ]

    def _advance_to_approved(self):
        plan_id = self._create_plan(self._default_items()).json()["data"]["id"]
        self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        self.client.post(f"/api/destruction-plans/{plan_id}/approve/", {"decision": "approved"}, format="json")
        return plan_id

    def test_full_workflow(self):
        plan_id = self._advance_to_approved()
        execute = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        self.assertEqual(execute.status_code, 200)
        data = execute.json()["data"]
        self.assertEqual(data["destroyed_count"], 2)
        self.assertEqual(data["blocked_count"], 0)
        self.assertEqual(data["failed_count"], 0)

        self.goods.refresh_from_db()
        self.goods2.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("9"))
        self.assertEqual(self.goods2.quantity, Decimal("3"))

        plan = DestructionPlan.objects.get(pk=plan_id)
        self.assertEqual(plan.status, "executed")
        self.assertIsNotNone(plan.executed_at)
        self.assertTrue(plan.plan_no.startswith("DEST-"))
        items = DestructionPlanItem.objects.filter(plan_id=plan_id)
        self.assertEqual({item.status for item in items}, {"destroyed"})
        self.assertTrue(all(item.destroyed_at for item in items))

    def test_stage_snapshots_recorded(self):
        plan_id = self._advance_to_approved()
        self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")

        snapshots = DestructionStageSnapshot.objects.filter(plan_id=plan_id)
        self.assertEqual({s.stage for s in snapshots}, {"plan", "review", "approve", "execute"})
        for snapshot in snapshots:
            self.assertEqual(len(snapshot.digest), 64)
            self.assertEqual(snapshot.summary["item_count"], 2)
            expected = hashlib.sha256(
                json.dumps(snapshot.summary, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            self.assertEqual(snapshot.digest, expected)

        execute_snapshot = snapshots.get(stage="execute")
        statuses = {item["status"] for item in execute_snapshot.summary["items"]}
        self.assertEqual(statuses, {"destroyed"})

        detail = self.client.get(f"/api/destruction-plans/{plan_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["data"]["snapshots"]), 4)

    def test_review_marks_ineligible_items(self):
        frozen_goods = Goods.objects.create(
            variety=self.variety, name="冻结物资", code="FRZ-001",
            quantity=Decimal("4"), custody_until=date.today() - timedelta(days=1),
            is_frozen=True, freeze_reason="涉案冻结",
        )
        unexpired_goods = Goods.objects.create(
            variety=self.variety, name="未到期物资", code="EXP-001",
            quantity=Decimal("4"), custody_until=date.today() + timedelta(days=30),
        )
        investigated_goods = Goods.objects.create(
            variety=self.variety, name="涉调查物资", code="INV-001",
            quantity=Decimal("4"), custody_until=date.today() - timedelta(days=1),
        )
        Investigation.objects.create(goods=investigated_goods, title="丢失调查", opened_by=self.user)
        checked_out_goods = Goods.objects.create(
            variety=self.variety, name="领用中物资", code="OUT-001",
            quantity=Decimal("4"), custody_until=date.today() - timedelta(days=1),
        )
        StockOut.objects.create(
            goods=checked_out_goods, operator=self.user, receiver="张三", quantity=Decimal("1")
        )
        no_custody_goods = Goods.objects.create(
            variety=self.variety, name="无期限物资", code="NOC-001", quantity=Decimal("4"),
        )

        plan_id = self._create_plan([
            {"goods": self.goods.id, "quantity": "1", "reason": "届满"},
            {"goods": frozen_goods.id, "quantity": "1", "reason": "届满"},
            {"goods": unexpired_goods.id, "quantity": "1", "reason": "届满"},
            {"goods": investigated_goods.id, "quantity": "1", "reason": "届满"},
            {"goods": checked_out_goods.id, "quantity": "1", "reason": "届满"},
            {"goods": no_custody_goods.id, "quantity": "1", "reason": "届满"},
        ]).json()["data"]["id"]

        review = self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        self.assertEqual(review.status_code, 200)
        results = {r["goods_code"]: r for r in review.json()["data"]["results"]}
        self.assertEqual(results["DEV-001"]["status"], "eligible")
        self.assertEqual(results["FRZ-001"]["status"], "ineligible")
        self.assertIn("冻结", results["FRZ-001"]["note"])
        self.assertIn("保管期限未届满", results["EXP-001"]["note"])
        self.assertIn("未结调查", results["INV-001"]["note"])
        self.assertIn("领用", results["OUT-001"]["note"])
        self.assertIn("保管期限未设定", results["NOC-001"]["note"])

        plan = DestructionPlan.objects.get(pk=plan_id)
        self.assertEqual(plan.status, "reviewed")

    def test_execute_rechecks_and_blocks_newly_frozen(self):
        plan_id = self._advance_to_approved()
        # 批准后物资被冻结，执行确认时必须拦截
        self.goods2.is_frozen = True
        self.goods2.freeze_reason = "执行前发现涉案"
        self.goods2.save()

        execute = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        self.assertEqual(execute.status_code, 200)
        data = execute.json()["data"]
        self.assertEqual(data["destroyed_count"], 1)
        self.assertEqual(data["blocked_count"], 1)

        self.goods.refresh_from_db()
        self.goods2.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("9"))
        self.assertEqual(self.goods2.quantity, Decimal("5"))

        blocked_item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods2)
        self.assertEqual(blocked_item.status, "blocked")
        self.assertIn("冻结", blocked_item.execute_note)
        plan = DestructionPlan.objects.get(pk=plan_id)
        self.assertEqual(plan.status, "executed")

    def test_execute_rechecks_investigation_and_checkout(self):
        goods_inv = Goods.objects.create(
            variety=self.variety, name="物资三", code="DEV-003",
            quantity=Decimal("6"), custody_until=date.today() - timedelta(days=1),
        )
        goods_out = Goods.objects.create(
            variety=self.variety, name="物资四", code="DEV-004",
            quantity=Decimal("6"), custody_until=date.today() - timedelta(days=1),
        )
        plan_id = self._create_plan([
            {"goods": goods_inv.id, "quantity": "1", "reason": "届满"},
            {"goods": goods_out.id, "quantity": "1", "reason": "届满"},
        ]).json()["data"]["id"]
        self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        self.client.post(f"/api/destruction-plans/{plan_id}/approve/", {"decision": "approved"}, format="json")

        # 批准后出现未结调查与新的领用申请
        Investigation.objects.create(goods=goods_inv, title="执行前立案", opened_by=self.user)
        StockOut.objects.create(goods=goods_out, operator=self.user, receiver="李四", quantity=Decimal("1"))

        execute = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        self.assertEqual(execute.status_code, 200)
        data = execute.json()["data"]
        self.assertEqual(data["destroyed_count"], 0)
        self.assertEqual(data["blocked_count"], 2)

        goods_inv.refresh_from_db()
        goods_out.refresh_from_db()
        self.assertEqual(goods_inv.quantity, Decimal("6"))
        self.assertEqual(goods_out.quantity, Decimal("6"))
        notes = set(
            DestructionPlanItem.objects.filter(plan_id=plan_id).values_list("execute_note", flat=True)
        )
        self.assertTrue(any("未结调查" in note for note in notes))
        self.assertTrue(any("领用" in note for note in notes))

    def test_batch_failure_does_not_affect_other_items(self):
        plan_id = self._advance_to_approved()
        original_save = Goods.save

        def flaky_save(instance, *args, **kwargs):
            if instance.pk == self.goods2.pk:
                raise RuntimeError("模拟数据库故障")
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(Goods, "save", flaky_save):
            execute = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")

        self.assertEqual(execute.status_code, 200)
        data = execute.json()["data"]
        self.assertEqual(data["destroyed_count"], 1)
        self.assertEqual(data["failed_count"], 1)

        self.goods.refresh_from_db()
        self.goods2.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("9"))
        self.assertEqual(self.goods2.quantity, Decimal("5"))

        failed_item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods2)
        self.assertEqual(failed_item.status, "failed")
        self.assertIn("处理异常", failed_item.execute_note)
        destroyed_item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods)
        self.assertEqual(destroyed_item.status, "destroyed")
        plan = DestructionPlan.objects.get(pk=plan_id)
        self.assertEqual(plan.status, "executed")

    def test_executed_plan_is_terminal(self):
        plan_id = self._advance_to_approved()
        self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")

        review = self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        approve = self.client.post(f"/api/destruction-plans/{plan_id}/approve/", {"decision": "approved"}, format="json")
        execute = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        self.assertEqual(review.status_code, 400)
        self.assertEqual(approve.status_code, 400)
        self.assertEqual(execute.status_code, 400)

        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("9"))

    def test_stage_order_enforced(self):
        plan_id = self._create_plan(self._default_items()).json()["data"]["id"]
        approve = self.client.post(f"/api/destruction-plans/{plan_id}/approve/", {"decision": "approved"}, format="json")
        execute = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        self.assertEqual(approve.status_code, 400)
        self.assertEqual(execute.status_code, 400)

        self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        review_again = self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        self.assertEqual(review_again.status_code, 400)
        execute_early = self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        self.assertEqual(execute_early.status_code, 400)

    def test_approve_requires_eligible_items(self):
        self.goods.is_frozen = True
        self.goods.freeze_reason = "涉案"
        self.goods.save()
        self.goods2.is_frozen = True
        self.goods2.freeze_reason = "涉案"
        self.goods2.save()

        plan_id = self._create_plan(self._default_items()).json()["data"]["id"]
        self.client.post(f"/api/destruction-plans/{plan_id}/review/", {}, format="json")
        approve = self.client.post(f"/api/destruction-plans/{plan_id}/approve/", {"decision": "approved"}, format="json")
        self.assertEqual(approve.status_code, 400)

        reject = self.client.post(f"/api/destruction-plans/{plan_id}/approve/", {"decision": "rejected"}, format="json")
        self.assertEqual(reject.status_code, 200)
        plan = DestructionPlan.objects.get(pk=plan_id)
        self.assertEqual(plan.status, "rejected")

    def test_correction_updates_metadata_only(self):
        plan_id = self._advance_to_approved()
        self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods)

        response = self.client.post(f"/api/destruction-plans/{plan_id}/corrections/", {
            "item": item.id,
            "changes": {"method": "焚烧", "remark": "按批准方式更正"},
            "reason": "销毁方式录入错误",
        }, format="json")
        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertEqual(item.method, "焚烧")
        self.assertEqual(item.remark, "按批准方式更正")
        correction = DestructionCorrection.objects.get(plan_id=plan_id)
        self.assertEqual(correction.changes["method"]["old"], "粉碎")
        self.assertEqual(correction.changes["method"]["new"], "焚烧")
        self.assertEqual(correction.reason, "销毁方式录入错误")

        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("9"))

    def test_correction_rejects_inventory_fields(self):
        plan_id = self._advance_to_approved()
        self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")
        item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods)

        response = self.client.post(f"/api/destruction-plans/{plan_id}/corrections/", {
            "item": item.id,
            "changes": {"quantity": "1"},
            "reason": "试图修改数量",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("元数据", response.json()["message"])

        item.refresh_from_db()
        self.assertEqual(item.quantity, Decimal("3"))
        self.assertEqual(DestructionCorrection.objects.filter(plan_id=plan_id).count(), 0)

    def test_correction_requires_executed_plan_and_destroyed_item(self):
        plan_id = self._advance_to_approved()
        item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods)

        not_executed = self.client.post(f"/api/destruction-plans/{plan_id}/corrections/", {
            "item": item.id, "changes": {"method": "焚烧"}, "reason": "录入错误",
        }, format="json")
        self.assertEqual(not_executed.status_code, 400)

        self.goods2.is_frozen = True
        self.goods2.freeze_reason = "涉案"
        self.goods2.save()
        self.client.post(f"/api/destruction-plans/{plan_id}/execute/", {}, format="json")

        blocked_item = DestructionPlanItem.objects.get(plan_id=plan_id, goods=self.goods2)
        blocked = self.client.post(f"/api/destruction-plans/{plan_id}/corrections/", {
            "item": blocked_item.id, "changes": {"method": "焚烧"}, "reason": "录入错误",
        }, format="json")
        self.assertEqual(blocked.status_code, 400)

    def test_create_plan_validation(self):
        empty = self._create_plan([])
        self.assertEqual(empty.status_code, 400)
        over_stock = self._create_plan([{"goods": self.goods.id, "quantity": "99", "reason": "届满"}])
        self.assertEqual(over_stock.status_code, 400)
        zero_qty = self._create_plan([{"goods": self.goods.id, "quantity": "0", "reason": "届满"}])
        self.assertEqual(zero_qty.status_code, 400)
        missing_goods = self._create_plan([{"goods": 99999, "quantity": "1", "reason": "届满"}])
        self.assertEqual(missing_goods.status_code, 400)
        duplicate = self._create_plan([
            {"goods": self.goods.id, "quantity": "1", "reason": "届满"},
            {"goods": self.goods.id, "quantity": "2", "reason": "届满"},
        ])
        self.assertEqual(duplicate.status_code, 400)

    def test_goods_cannot_be_in_two_open_plans(self):
        first = self._create_plan(self._default_items())
        self.assertEqual(first.status_code, 200)
        second = self._create_plan(self._default_items())
        self.assertEqual(second.status_code, 400)

    def test_freeze_and_investigation_endpoints(self):
        no_reason = self.client.post(f"/api/goods/{self.goods.id}/freeze/", {"frozen": True}, format="json")
        self.assertEqual(no_reason.status_code, 400)
        freeze = self.client.post(f"/api/goods/{self.goods.id}/freeze/", {"frozen": True, "reason": "涉案"}, format="json")
        self.assertEqual(freeze.status_code, 200)
        self.goods.refresh_from_db()
        self.assertTrue(self.goods.is_frozen)
        self.assertEqual(self.goods.freeze_reason, "涉案")
        unfreeze = self.client.post(f"/api/goods/{self.goods.id}/freeze/", {"frozen": False}, format="json")
        self.assertEqual(unfreeze.status_code, 200)
        self.goods.refresh_from_db()
        self.assertFalse(self.goods.is_frozen)
        self.assertEqual(self.goods.freeze_reason, "")

        opened = self.client.post("/api/investigations/", {"goods": self.goods.id, "title": "丢失调查"}, format="json")
        self.assertEqual(opened.status_code, 200)
        investigation_id = opened.json()["data"]["id"]
        closed = self.client.post(f"/api/investigations/{investigation_id}/close/", {}, format="json")
        self.assertEqual(closed.status_code, 200)
        close_again = self.client.post(f"/api/investigations/{investigation_id}/close/", {}, format="json")
        self.assertEqual(close_again.status_code, 400)

    def test_destruction_requires_authentication(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get("/api/destruction-plans/").status_code, 401)
        self.assertEqual(anonymous.post("/api/destruction-plans/", {}, format="json").status_code, 401)
