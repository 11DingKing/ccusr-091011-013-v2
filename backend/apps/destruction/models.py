"""
物资销毁管理模型

四阶段状态机：
    planned（销毁计划）→ reviewed（资格复核）→ approved（批准）→ executed（执行确认，终态）
    复核/批准整批驳回时进入 abandoned（终态）。

每个阶段落库一份不可变清单快照（DestructionStageSnapshot）；
每个清单项独立持有状态，批量处理时单项失败只影响该项。
"""
from django.conf import settings
from django.db import models


# ==================== 状态常量 ====================

class PlanStatus(models.TextChoices):
    PLANNED = 'planned', '销毁计划'
    REVIEWED = 'reviewed', '资格复核'
    APPROVED = 'approved', '已批准'
    EXECUTED = 'executed', '已执行'
    ABANDONED = 'abandoned', '已终止'


class ItemStatus(models.TextChoices):
    INCLUDED = 'included', '已列入'
    ELIGIBLE = 'eligible', '复核合格'
    EXCLUDED = 'excluded', '复核剔除'
    APPROVED = 'approved', '批准销毁'
    REJECTED = 'rejected', '批准拒绝'
    BLOCKED = 'blocked', '执行拦截'
    DESTROYED = 'destroyed', '已销毁'


class SnapshotStage(models.TextChoices):
    PLANNING = 'planning', '销毁计划'
    REVIEW = 'review', '资格复核'
    APPROVAL = 'approval', '批准'
    EXECUTION = 'execution', '执行确认'


# 项级终态：处于这些状态的项不会再被批量推进
ITEM_TERMINAL_STATUSES = (
    ItemStatus.EXCLUDED,
    ItemStatus.REJECTED,
    ItemStatus.BLOCKED,
    ItemStatus.DESTROYED,
)

# 计划级终态
PLAN_TERMINAL_STATUSES = (PlanStatus.EXECUTED, PlanStatus.ABANDONED)

# 更正允许修改的物资元数据白名单（库存数量、生命周期状态永不可经更正恢复）
CORRECTABLE_FIELDS = ['name', 'code', 'specification', 'location', 'remark']
CORRECTABLE_FIELD_LABELS = {
    'name': '货物名称',
    'code': '货物编码',
    'specification': '规格型号',
    'location': '存放位置',
    'remark': '备注',
}


class DestructionPlan(models.Model):
    """销毁计划"""
    plan_no = models.CharField('计划编号', max_length=40, unique=True)
    title = models.CharField('计划标题', max_length=200)
    scheduled_date = models.DateField('拟销毁日期', null=True, blank=True)
    reason = models.TextField('销毁事由', blank=True)

    status = models.CharField(
        '计划状态', max_length=20,
        choices=PlanStatus.choices, default=PlanStatus.PLANNED
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='destruction_plans', verbose_name='计划制定人'
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_destruction_plans', verbose_name='复核人'
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_destruction_plans', verbose_name='批准人'
    )
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='executed_destruction_plans', verbose_name='执行确认人'
    )

    review_remark = models.TextField('复核意见', blank=True)
    approval_remark = models.TextField('批准意见', blank=True)
    execute_remark = models.TextField('执行备注', blank=True)

    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    approved_at = models.DateTimeField('批准时间', null=True, blank=True)
    executed_at = models.DateTimeField('执行时间', null=True, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ds_destruction_plan'
        verbose_name = '销毁计划'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.plan_no} - {self.title}"

    @property
    def is_terminal(self):
        return self.status in PLAN_TERMINAL_STATUSES

    @property
    def item_count(self):
        return self.items.count()

    @property
    def destroyed_count(self):
        return self.items.filter(status=ItemStatus.DESTROYED).count()


class DestructionItem(models.Model):
    """销毁清单条目（逐项独立状态，失败只落到单项）"""
    plan = models.ForeignKey(
        DestructionPlan, on_delete=models.CASCADE,
        related_name='items', verbose_name='销毁计划'
    )
    goods = models.ForeignKey(
        'warehouse.Goods', on_delete=models.PROTECT,
        related_name='destruction_items', verbose_name='物资'
    )

    # 列入时的清单快照值
    goods_code = models.CharField('列入时物资编码', max_length=50)
    goods_name = models.CharField('列入时物资名称', max_length=200)
    quantity_at_plan = models.DecimalField('列入时库存数量', max_digits=12, decimal_places=2)

    status = models.CharField(
        '条目状态', max_length=20,
        choices=ItemStatus.choices, default=ItemStatus.INCLUDED
    )
    # 拦截/剔除/拒绝原因列表，如 ["物资处于冻结中", "存在未结调查"]
    block_reasons = models.JSONField('拦截原因', default=list, blank=True)
    # 列入计划时即发现的软性风险（不阻止建计划，供复核关注）
    plan_warnings = models.JSONField('列入时风险提示', default=list, blank=True)

    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    approved_at = models.DateTimeField('批准时间', null=True, blank=True)
    destroyed_at = models.DateTimeField('销毁时间', null=True, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ds_destruction_item'
        verbose_name = '销毁清单条目'
        verbose_name_plural = verbose_name
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['plan', 'goods'], name='uniq_goods_per_destruction_plan'
            ),
        ]

    def __str__(self):
        return f"{self.goods_code} - {self.get_status_display()}"

    @property
    def is_terminal(self):
        return self.status in ITEM_TERMINAL_STATUSES


class DestructionStageSnapshot(models.Model):
    """各阶段的清单摘要快照（只追加，不修改）"""
    plan = models.ForeignKey(
        DestructionPlan, on_delete=models.CASCADE,
        related_name='snapshots', verbose_name='销毁计划'
    )
    stage = models.CharField('阶段', max_length=20, choices=SnapshotStage.choices)
    summary = models.JSONField('清单摘要', default=dict)
    checksum = models.CharField('摘要校验值', max_length=64, blank=True)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='destruction_snapshots', verbose_name='操作人'
    )
    note = models.TextField('阶段备注', blank=True)
    created_at = models.DateTimeField('生成时间', auto_now_add=True)

    class Meta:
        db_table = 'ds_stage_snapshot'
        verbose_name = '销毁阶段清单快照'
        verbose_name_plural = verbose_name
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['plan', 'stage'])]

    def __str__(self):
        return f"{self.plan.plan_no} - {self.get_stage_display()}"


class DestructionCorrection(models.Model):
    """销毁后元数据更正记录（只改元数据，不恢复库存、不改变终态）"""
    plan = models.ForeignKey(
        DestructionPlan, on_delete=models.CASCADE,
        related_name='corrections', verbose_name='销毁计划'
    )
    item = models.ForeignKey(
        DestructionItem, on_delete=models.PROTECT,
        related_name='corrections', verbose_name='销毁条目'
    )
    goods = models.ForeignKey(
        'warehouse.Goods', on_delete=models.PROTECT,
        related_name='destruction_corrections', verbose_name='物资'
    )
    field_name = models.CharField('更正字段', max_length=50)
    old_value = models.TextField('更正前值', blank=True)
    new_value = models.TextField('更正后值', blank=True)
    reason = models.TextField('更正原因')
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='destruction_corrections', verbose_name='更正人'
    )
    created_at = models.DateTimeField('更正时间', auto_now_add=True)

    class Meta:
        db_table = 'ds_destruction_correction'
        verbose_name = '销毁后元数据更正'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.plan.plan_no} - {self.goods.code} - {self.field_name}"

    @property
    def field_label(self):
        return CORRECTABLE_FIELD_LABELS.get(self.field_name, self.field_name)
