"""
库房管理模型
"""
from django.db import models
from apps.authentication.models import User


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    is_frozen = models.BooleanField('是否冻结', default=False)
    freeze_reason = models.CharField('冻结原因', max_length=200, blank=True)
    custody_until = models.DateField('保管期限届满日', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)
    
    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='操作人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    
    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"


class Investigation(models.Model):
    """调查记录模型"""
    STATUS_CHOICES = [
        ('open', '调查中'),
        ('closed', '已结案'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='investigations', verbose_name='货物'
    )
    title = models.CharField('调查事项', max_length=100)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='open')
    opened_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='opened_investigations', verbose_name='立案人'
    )
    closed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='closed_investigations', verbose_name='结案人'
    )
    opened_at = models.DateTimeField('立案时间', auto_now_add=True)
    closed_at = models.DateTimeField('结案时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        db_table = 'wh_investigation'
        verbose_name = '调查记录'
        verbose_name_plural = verbose_name
        ordering = ['-opened_at']

    def __str__(self):
        return f"{self.goods.name} - {self.title}"


class DestructionPlan(models.Model):
    """销毁计划模型（计划 -> 复核 -> 批准 -> 执行，单向流转）"""
    STATUS_CHOICES = [
        ('planning', '待复核'),
        ('reviewed', '待批准'),
        ('approved', '待执行'),
        ('rejected', '已驳回'),
        ('executed', '已执行'),
    ]

    plan_no = models.CharField('计划编号', max_length=30, unique=True)
    title = models.CharField('计划名称', max_length=100)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='planning')
    remark = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='destruction_plans', verbose_name='创建人'
    )
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_destruction_plans', verbose_name='复核人'
    )
    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    review_remark = models.CharField('复核意见', max_length=300, blank=True)
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_destruction_plans', verbose_name='批准人'
    )
    approved_at = models.DateTimeField('批准时间', null=True, blank=True)
    approve_remark = models.CharField('批准意见', max_length=300, blank=True)
    executed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='executed_destruction_plans', verbose_name='执行人'
    )
    executed_at = models.DateTimeField('执行时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_destruction_plan'
        verbose_name = '销毁计划'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.plan_no} - {self.title}"


class DestructionPlanItem(models.Model):
    """销毁计划明细模型"""
    STATUS_CHOICES = [
        ('pending', '待复核'),
        ('eligible', '复核通过'),
        ('ineligible', '复核不通过'),
        ('destroyed', '已销毁'),
        ('blocked', '执行拦截'),
        ('failed', '处理失败'),
    ]

    plan = models.ForeignKey(
        DestructionPlan, on_delete=models.CASCADE,
        related_name='items', verbose_name='销毁计划'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='destruction_items', verbose_name='货物'
    )
    quantity = models.DecimalField('销毁数量', max_digits=12, decimal_places=2)
    reason = models.CharField('销毁原因', max_length=200)
    method = models.CharField('销毁方式', max_length=50, blank=True)
    remark = models.TextField('备注', blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    review_note = models.CharField('复核结论', max_length=300, blank=True)
    execute_note = models.CharField('执行说明', max_length=300, blank=True)
    destroyed_at = models.DateTimeField('销毁时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_destruction_plan_item'
        verbose_name = '销毁计划明细'
        verbose_name_plural = verbose_name
        ordering = ['id']
        unique_together = ['plan', 'goods']

    def __str__(self):
        return f"{self.plan.plan_no} - {self.goods.name}"


class DestructionStageSnapshot(models.Model):
    """销毁阶段清单摘要模型（每个阶段留存当时的清单及摘要值）"""
    STAGE_CHOICES = [
        ('plan', '销毁计划'),
        ('review', '资格复核'),
        ('approve', '批准'),
        ('execute', '执行确认'),
    ]

    plan = models.ForeignKey(
        DestructionPlan, on_delete=models.CASCADE,
        related_name='snapshots', verbose_name='销毁计划'
    )
    stage = models.CharField('阶段', max_length=20, choices=STAGE_CHOICES)
    summary = models.JSONField('清单摘要')
    digest = models.CharField('摘要值', max_length=64)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='destruction_snapshots', verbose_name='操作人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_destruction_stage_snapshot'
        verbose_name = '销毁阶段清单摘要'
        verbose_name_plural = verbose_name
        ordering = ['created_at']
        unique_together = ['plan', 'stage']

    def __str__(self):
        return f"{self.plan.plan_no} - {self.get_stage_display()}"


class DestructionCorrection(models.Model):
    """销毁更正模型（仅更正元数据，不恢复库存）"""
    plan = models.ForeignKey(
        DestructionPlan, on_delete=models.CASCADE,
        related_name='corrections', verbose_name='销毁计划'
    )
    item = models.ForeignKey(
        DestructionPlanItem, on_delete=models.CASCADE,
        related_name='corrections', verbose_name='销毁明细'
    )
    changes = models.JSONField('更正内容')
    reason = models.CharField('更正原因', max_length=300)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='destruction_corrections', verbose_name='更正人'
    )
    created_at = models.DateTimeField('更正时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_destruction_correction'
        verbose_name = '销毁更正'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.plan.plan_no} - {self.item.goods.name} 更正"
