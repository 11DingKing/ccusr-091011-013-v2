"""
物资销毁业务服务层

所有阶段流转均在数据库事务中完成；批量处理按条目使用保存点隔离，
任一条目失败只回滚该条目并记录明确的失败状态/原因，其余条目照常推进。
"""
import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.warehouse.models import Goods

from .models import (
    CORRECTABLE_FIELDS,
    CORRECTABLE_FIELD_LABELS,
    DestructionCorrection,
    DestructionItem,
    DestructionPlan,
    DestructionStageSnapshot,
    ItemStatus,
    PLAN_TERMINAL_STATUSES,
    PlanStatus,
    SnapshotStage,
)


class DestructionError(Exception):
    """销毁流程业务错误（消息可直接返回给调用方）"""


# ==================== 工具函数 ====================

def _canonical_json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _make_checksum(summary):
    return hashlib.sha256(_canonical_json(summary).encode('utf-8')).hexdigest()


def generate_plan_no(today=None):
    """生成计划编号：DS + 年月日 + 当日序号"""
    today = today or timezone.localdate()
    prefix = f"DS{today.strftime('%Y%m%d')}"
    existing = DestructionPlan.objects.filter(plan_no__startswith=prefix).count()
    return f"{prefix}{existing + 1:04d}"


def check_eligibility(goods):
    """
    销毁资格检查。返回 (passed, reasons)。
    执行前必须重跑：冻结、未结调查、未结领用，另含终态、库存与保管期限检查。
    """
    reasons = []
    if goods.is_destroyed:
        reasons.append('物资已处于销毁终态')
    if goods.is_frozen:
        reasons.append('物资处于冻结中')
    if goods.has_open_investigation:
        reasons.append('物资存在未结调查')
    if goods.has_open_stock_out:
        reasons.append('物资存在未结领用')
    if not goods.is_retention_expired:
        if not goods.retention_expire_date:
            reasons.append('未登记保管期限届满日')
        else:
            reasons.append('保管期限尚未届满')
    if goods.quantity <= 0:
        reasons.append('库存数量为零，无可销毁物资')
    return (len(reasons) == 0, reasons)


def build_summary(plan):
    """构造当前清单摘要（逐项状态 + 数量汇总 + 拦截分类）"""
    items = list(plan.items.all())
    by_status = {}
    total_quantity = Decimal('0')
    item_payloads = []
    blocked_kinds = {'frozen': [], 'investigation': [], 'open_stock_out': [], 'other': []}

    for item in items:
        by_status[item.status] = by_status.get(item.status, 0) + 1
        total_quantity += item.quantity_at_plan
        payload = {
            'item_id': item.id,
            'goods_id': item.goods_id,
            'code': item.goods_code,
            'name': item.goods_name,
            'quantity': str(item.quantity_at_plan),
            'status': item.status,
            'reasons': list(item.block_reasons or []),
        }
        item_payloads.append(payload)
        if item.block_reasons:
            joined = ' '.join(item.block_reasons)
            if '冻结' in joined:
                blocked_kinds['frozen'].append(item.id)
            if '未结调查' in joined:
                blocked_kinds['investigation'].append(item.id)
            if '未结领用' in joined:
                blocked_kinds['open_stock_out'].append(item.id)

    return {
        'plan_no': plan.plan_no,
        'plan_status': plan.status,
        'generated_at': timezone.now().isoformat(),
        'total': len(items),
        'by_status': by_status,
        'total_quantity': str(total_quantity),
        'checks': blocked_kinds,
        'items': item_payloads,
    }


@transaction.atomic
def save_snapshot(plan, stage, operator, note=''):
    """保存某阶段当时的清单摘要（只追加）"""
    summary = build_summary(plan)
    return DestructionStageSnapshot.objects.create(
        plan=plan,
        stage=stage,
        summary=summary,
        checksum=_make_checksum(summary),
        operator=operator,
        note=note,
    )


# ==================== 阶段一：销毁计划 ====================

@transaction.atomic
def create_plan(*, operator, title, goods_ids, scheduled_date=None, reason=''):
    """
    建立销毁计划。
    - 已销毁、已在其它进行中计划内的物资不得列入（硬性拦截）；
    - 冻结/未结调查/未结领用/期限未届满只作风险提示随项记录，在复核、执行前再拦截。
    """
    if not title or not title.strip():
        raise DestructionError('计划标题不能为空')
    if not goods_ids:
        raise DestructionError('请至少选择一项物资列入销毁清单')

    unique_ids = list(dict.fromkeys(goods_ids))
    goods_list = list(Goods.objects.filter(pk__in=unique_ids).select_related(
        'variety__category__unit'))
    if len(goods_list) != len(unique_ids):
        found = {g.id for g in goods_list}
        missing = [gid for gid in unique_ids if gid not in found]
        raise DestructionError(f'以下物资不存在：{missing}')

    active_items = DestructionItem.objects.filter(
        goods_id__in=unique_ids,
    ).exclude(
        plan__status__in=PLAN_TERMINAL_STATUSES
    ).select_related('goods')
    if active_items.exists():
        codes = [it.goods.code for it in active_items]
        raise DestructionError(f'物资已存在进行中的销毁计划：{codes}')

    plan = DestructionPlan.objects.create(
        plan_no=generate_plan_no(),
        title=title.strip(),
        scheduled_date=scheduled_date,
        reason=reason,
        created_by=operator,
    )

    rejected = []
    for goods in goods_list:
        if goods.is_destroyed:
            rejected.append({'goods_id': goods.id, 'code': goods.code,
                             'reason': '物资已处于销毁终态，不得列入'})
            continue

        _, reasons = check_eligibility(goods)
        # 期限问题留给复核拦截；列入时只对动态管控状态做风险提示
        warnings = [r for r in reasons if r in (
            '物资处于冻结中', '物资存在未结调查', '物资存在未结领用')]
        DestructionItem.objects.create(
            plan=plan,
            goods=goods,
            goods_code=goods.code,
            goods_name=goods.name,
            quantity_at_plan=goods.quantity,
            plan_warnings=warnings,
        )

    if plan.items.count() == 0:
        raise DestructionError('没有可列入的物资，计划未创建')

    snapshot = save_snapshot(plan, SnapshotStage.PLANNING, operator,
                             note='销毁计划建立时清单摘要')
    return plan, snapshot, rejected


# ==================== 阶段二：资格复核 ====================

@transaction.atomic
def submit_review(*, plan_id, operator, remark=''):
    """
    资格复核：逐项重跑资格检查。
    合格 → eligible；不合格 → excluded 并记录原因。单项失败不影响其他项。
    """
    plan = DestructionPlan.objects.select_for_update().filter(pk=plan_id).first()
    if plan is None:
        raise DestructionError('销毁计划不存在')
    if plan.status != PlanStatus.PLANNED:
        raise DestructionError(f'当前状态（{plan.get_status_display()}）不可提交复核')

    items = list(plan.items.select_for_update().all())
    eligible_ids, excluded = [], []
    for item in items:
        try:
            with transaction.atomic():  # 外层已有事务，嵌套块使用保存点隔离单项
                goods = item.goods
                passed, reasons = check_eligibility(goods)
                if passed:
                    item.status = ItemStatus.ELIGIBLE
                    item.block_reasons = []
                    item.reviewed_at = timezone.now()
                    item.save(update_fields=['status', 'block_reasons', 'reviewed_at'])
                    eligible_ids.append(item.id)
                else:
                    item.status = ItemStatus.EXCLUDED
                    item.block_reasons = reasons
                    item.reviewed_at = timezone.now()
                    item.save(update_fields=['status', 'block_reasons', 'reviewed_at'])
                    excluded.append({'item_id': item.id, 'code': item.goods_code, 'reasons': reasons})
        except Exception as exc:  # 单项失败隔离：该项保持原状并登记为剔除
            item.status = ItemStatus.EXCLUDED
            item.block_reasons = [f'复核处理异常：{exc}']
            item.reviewed_at = timezone.now()
            item.save(update_fields=['status', 'block_reasons', 'reviewed_at'])
            excluded.append({'item_id': item.id, 'code': item.goods_code,
                             'reasons': item.block_reasons})

    plan.status = PlanStatus.REVIEWED
    plan.reviewed_by = operator
    plan.review_remark = remark
    plan.reviewed_at = timezone.now()
    plan.save(update_fields=['status', 'reviewed_by', 'review_remark', 'reviewed_at'])

    snapshot = save_snapshot(
        plan, SnapshotStage.REVIEW, operator,
        note=f'资格复核：合格 {len(eligible_ids)} 项，剔除 {len(excluded)} 项')
    return plan, snapshot, excluded


# ==================== 阶段三：批准 ====================

@transaction.atomic
def decide_plan(*, plan_id, operator, approved, item_decisions=None, remark=''):
    """
    批准决定：
    - approved=True：逐项决定（默认全部合格项批准）；可单独拒绝某项并注明原因；
    - approved=False：整批驳回，计划进入 abandoned 终态。
    至少批准一项才能进入 approved 状态。
    """
    plan = DestructionPlan.objects.select_for_update().filter(pk=plan_id).first()
    if plan is None:
        raise DestructionError('销毁计划不存在')
    if plan.status != PlanStatus.REVIEWED:
        raise DestructionError(f'当前状态（{plan.get_status_display()}）不可作出批准决定')

    items = list(plan.items.select_for_update().all())
    item_decisions = item_decisions or {}

    if not approved:
        for item in items:
            if item.status in (ItemStatus.ELIGIBLE, ItemStatus.INCLUDED):
                item.status = ItemStatus.REJECTED
                item.block_reasons = ['整批驳回']
                item.save(update_fields=['status', 'block_reasons'])
        plan.status = PlanStatus.ABANDONED
        plan.approved_by = operator
        plan.approval_remark = remark or '整批驳回'
        plan.approved_at = timezone.now()
        plan.save(update_fields=['status', 'approved_by', 'approval_remark', 'approved_at'])
        snapshot = save_snapshot(plan, SnapshotStage.APPROVAL, operator,
                                 note='整批驳回，计划终止')
        return plan, snapshot, []

    approved_items, rejected_items = [], []
    for item in items:
        if item.status != ItemStatus.ELIGIBLE:
            continue  # 已剔除项不参与批准
        try:
            with transaction.atomic():  # 外层已有事务，嵌套块使用保存点隔离单项
                decision = item_decisions.get(str(item.id), item_decisions.get(item.id))
                item_approved = True
                item_reason = ''
                if decision is not None:
                    item_approved = bool(decision.get('approved', True))
                    item_reason = str(decision.get('reason', '') or '')
                if item_approved:
                    item.status = ItemStatus.APPROVED
                    item.block_reasons = []
                else:
                    item.status = ItemStatus.REJECTED
                    item.block_reasons = [item_reason or '批准人拒绝']
                item.approved_at = timezone.now()
                item.save(update_fields=['status', 'block_reasons', 'approved_at'])
                (approved_items if item_approved else rejected_items).append(item.id)
        except Exception as exc:
            # 单项失败隔离：该项标记为拒绝而非含糊停留
            item.status = ItemStatus.REJECTED
            item.block_reasons = [f'批准处理异常：{exc}']
            item.approved_at = timezone.now()
            item.save(update_fields=['status', 'block_reasons', 'approved_at'])
            rejected_items.append(item.id)

    if not approved_items:
        raise DestructionError('未批准任何销毁项；如确需终止请使用驳回操作')

    plan.status = PlanStatus.APPROVED
    plan.approved_by = operator
    plan.approval_remark = remark
    plan.approved_at = timezone.now()
    plan.save(update_fields=['status', 'approved_by', 'approval_remark', 'approved_at'])

    snapshot = save_snapshot(
        plan, SnapshotStage.APPROVAL, operator,
        note=f'批准：同意销毁 {len(approved_items)} 项，拒绝 {len(rejected_items)} 项')
    return plan, snapshot, rejected_items


# ==================== 阶段四：执行确认 ====================

@transaction.atomic
def confirm_execution(*, plan_id, operator, remark=''):
    """
    执行确认（不可逆）：
    执行前对每个已批准（或上轮被拦截）项再次检查冻结、未结调查、未结领用等；
    通过项置为 destroyed 并把物资 quantity 清零、生命周期置为终态；
    未通过项置为 blocked 并记录原因，绝不销毁。
    全部拦截时计划保持 approved，不产生任何销毁；管控解除后可再次执行确认，
    被拦截项会重新参加资格检查。
    """
    plan = DestructionPlan.objects.select_for_update().filter(pk=plan_id).first()
    if plan is None:
        raise DestructionError('销毁计划不存在')
    if plan.status != PlanStatus.APPROVED:
        raise DestructionError(f'当前状态（{plan.get_status_display()}）不可执行确认')

    items = list(plan.items.select_for_update().filter(
        status__in=[ItemStatus.APPROVED, ItemStatus.BLOCKED]))
    if not items:
        raise DestructionError('没有已批准待执行的销毁项')

    destroyed, blocked = [], []
    for item in items:
        try:
            with transaction.atomic():  # 外层已有事务，嵌套块使用保存点隔离单项
                goods = Goods.objects.select_for_update().get(pk=item.goods_id)
                passed, reasons = check_eligibility(goods)
                if not passed:
                    item.status = ItemStatus.BLOCKED
                    item.block_reasons = reasons
                    item.save(update_fields=['status', 'block_reasons'])
                    blocked.append({'item_id': item.id, 'code': item.goods_code, 'reasons': reasons})
                else:
                    goods.quantity = Decimal('0')
                    goods.lifecycle_status = 'destroyed'
                    goods.is_active = False
                    goods.save(update_fields=['quantity', 'lifecycle_status', 'is_active'])
                    item.status = ItemStatus.DESTROYED
                    item.block_reasons = []
                    item.destroyed_at = timezone.now()
                    item.save(update_fields=['status', 'block_reasons', 'destroyed_at'])
                    destroyed.append(item.id)
        except Exception as exc:
            # 单项失败隔离：标记 blocked，绝不让物资状态与条目状态不一致地停留
            item.status = ItemStatus.BLOCKED
            item.block_reasons = [f'执行处理异常：{exc}']
            item.save(update_fields=['status', 'block_reasons'])
            blocked.append({'item_id': item.id, 'code': item.goods_code,
                            'reasons': item.block_reasons})

    if not destroyed:
        snapshot = save_snapshot(
            plan, SnapshotStage.EXECUTION, operator,
            note=f'执行前检查未通过，全部 {len(blocked)} 项拦截，未发生销毁')
        return plan, snapshot, blocked, destroyed

    plan.status = PlanStatus.EXECUTED
    plan.executed_by = operator
    plan.execute_remark = remark
    plan.executed_at = timezone.now()
    plan.save(update_fields=['status', 'executed_by', 'execute_remark', 'executed_at'])

    snapshot = save_snapshot(
        plan, SnapshotStage.EXECUTION, operator,
        note=f'执行确认：销毁 {len(destroyed)} 项，拦截 {len(blocked)} 项')
    return plan, snapshot, blocked, destroyed


# ==================== 销毁后元数据更正 ====================

# 各可更正字段的长度上限（与 Goods 模型保持一致）
_FIELD_MAX_LENGTHS = {
    'name': 200,
    'code': 50,
    'specification': 200,
    'location': 100,
    'remark': None,
}


@transaction.atomic
def correct_metadata(*, item_id, operator, changes, reason):
    """
    销毁后录入错误更正：只允许修改白名单元数据字段，
    不恢复库存数量、不改变销毁终态。每个字段一条更正记录。
    changes: {field_name: new_value}
    """
    if not reason or not reason.strip():
        raise DestructionError('必须填写更正原因')
    if not changes:
        raise DestructionError('没有需要更正的字段')

    item = DestructionItem.objects.select_for_update().select_related(
        'plan', 'goods').filter(pk=item_id).first()
    if item is None:
        raise DestructionError('销毁条目不存在')
    if item.status != ItemStatus.DESTROYED:
        raise DestructionError('仅已销毁的条目可提交更正')

    goods = Goods.objects.select_for_update().get(pk=item.goods_id)
    if not goods.is_destroyed or goods.quantity != 0:
        # 防护性校验：终态与零库存是更正的前提，方法本身绝不触碰这两项
        raise DestructionError('物资未处于不可逆终态，不能走更正流程')

    unknown = [f for f in changes if f not in CORRECTABLE_FIELDS]
    if unknown:
        raise DestructionError(f'不允许更正的字段：{unknown}；允许字段：{CORRECTABLE_FIELDS}')

    records = []
    for field_name, new_value in changes.items():
        new_value = '' if new_value is None else str(new_value)
        max_length = _FIELD_MAX_LENGTHS.get(field_name)
        if max_length and len(new_value) > max_length:
            raise DestructionError(
                f'{CORRECTABLE_FIELD_LABELS[field_name]}长度不能超过 {max_length} 字')
        if field_name == 'code' and not new_value.strip():
            raise DestructionError('货物编码不能为空')
        if field_name == 'name' and not new_value.strip():
            raise DestructionError('货物名称不能为空')
        if field_name == 'code':
            if Goods.objects.filter(code=new_value).exclude(pk=goods.pk).exists():
                raise DestructionError('货物编码已存在')

        old_value = str(getattr(goods, field_name) or '')
        if old_value == new_value:
            continue

        setattr(goods, field_name, new_value)
        goods.save(update_fields=[field_name, 'updated_at'])

        # 条目上的 goods_code/goods_name 是列入时快照，保持不变；
        # 编码/名称的前后值由更正记录留痕。
        records.append(DestructionCorrection.objects.create(
            plan=item.plan, item=item, goods=goods,
            field_name=field_name, old_value=old_value, new_value=new_value,
            reason=reason.strip(), operator=operator,
        ))

    if not records:
        raise DestructionError('所有字段与现值一致，无实际更正')
    return item, records
