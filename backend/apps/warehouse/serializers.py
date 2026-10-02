"""
仓库管理序列化器
"""
from rest_framework import serializers
from .models import (
    Unit, Category, Variety, Goods, StockIn, StockOut, Warning, Approval,
    Investigation, DestructionPlan, DestructionPlanItem,
    DestructionStageSnapshot, DestructionCorrection,
)


class UnitSerializer(serializers.ModelSerializer):
    """单位序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UnitCreateSerializer(serializers.Serializer):
    """单位创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=5, required=True, error_messages={
        'required': '请输入单位名称',
        'blank': '单位名称不能为空',
        'min_length': '单位名称至少1个字',
        'max_length': '单位名称最多5个字',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Unit.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('单位名称已存在')
        else:
            if Unit.objects.filter(name=value).exists():
                raise serializers.ValidationError('单位名称已存在')
        return value


class CategorySerializer(serializers.ModelSerializer):
    """品类序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'unit', 'unit_name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CategoryCreateSerializer(serializers.Serializer):
    """品类创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=10, required=True, error_messages={
        'required': '请输入品类名称',
        'blank': '品类名称不能为空',
        'min_length': '品类名称至少1个字',
        'max_length': '品类名称最多10个字',
    })
    unit = serializers.IntegerField(required=True, error_messages={
        'required': '请选择单位',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Category.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('品类名称已存在')
        else:
            if Category.objects.filter(name=value).exists():
                raise serializers.ValidationError('品类名称已存在')
        return value
    
    def validate_unit(self, value):
        if not Unit.objects.filter(pk=value).exists():
            raise serializers.ValidationError('单位不存在')
        return value


class VarietySerializer(serializers.ModelSerializer):
    """品种序列化器"""
    is_in_stock = serializers.BooleanField(read_only=True)
    unit_name = serializers.CharField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Variety
        fields = [
            'id', 'name', 'category', 'category_name', 'unit_name',
            'is_in_stock', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class VarietyCreateSerializer(serializers.Serializer):
    """品种创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=20, required=True, error_messages={
        'required': '请输入品种名称',
        'blank': '品种名称不能为空',
        'min_length': '品种名称至少1个字',
        'max_length': '品种名称最多20个字',
    })
    category = serializers.IntegerField(required=True, error_messages={
        'required': '请选择品类',
    })
    
    def validate_category(self, value):
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value
    
    def validate(self, data):
        instance = self.context.get('instance')
        name = data['name']
        category_id = data['category']
        
        if instance:
            if Variety.objects.filter(name=name, category_id=category_id).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        else:
            if Variety.objects.filter(name=name, category_id=category_id).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        return data


class GoodsSerializer(serializers.ModelSerializer):
    """货物序列化器"""
    variety_name = serializers.CharField(source='variety.name', read_only=True)
    category_name = serializers.CharField(source='variety.category.name', read_only=True)
    unit_name = serializers.CharField(source='variety.category.unit.name', read_only=True)
    is_warning = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Goods
        fields = [
            'id', 'name', 'code', 'variety', 'variety_name',
            'category_name', 'unit_name', 'specification',
            'quantity', 'warning_threshold', 'location',
            'remark', 'is_active', 'is_warning',
            'is_frozen', 'freeze_reason', 'custody_until',
            'created_at', 'updated_at'
        ]


class StockInSerializer(serializers.ModelSerializer):
    """入库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    
    class Meta:
        model = StockIn
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'quantity', 'batch_no', 'supplier', 'stock_in_time', 'remark'
        ]


class StockOutSerializer(serializers.ModelSerializer):
    """出库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'receiver', 'receiver_dept', 'quantity', 'status', 'status_display',
            'stock_out_time', 'remark', 'created_at'
        ]


class WarningSerializer(serializers.ModelSerializer):
    """预警记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    
    class Meta:
        model = Warning
        fields = [
            'id', 'goods', 'goods_name', 'type', 'type_display',
            'message', 'is_read', 'created_at'
        ]


class ApprovalSerializer(serializers.ModelSerializer):
    """审批记录序列化器"""
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Approval
        fields = [
            'id', 'stock_out', 'approver', 'approver_name',
            'status', 'status_display', 'remark', 'created_at', 'updated_at'
        ]


class InvestigationSerializer(serializers.ModelSerializer):
    """调查记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    opened_by_name = serializers.CharField(source='opened_by.username', read_only=True)
    closed_by_name = serializers.CharField(source='closed_by.username', read_only=True)

    class Meta:
        model = Investigation
        fields = [
            'id', 'goods', 'goods_name', 'goods_code', 'title',
            'status', 'status_display', 'opened_by', 'opened_by_name',
            'closed_by', 'closed_by_name', 'opened_at', 'closed_at', 'remark'
        ]


class InvestigationCreateSerializer(serializers.Serializer):
    """调查立案序列化器"""
    goods = serializers.IntegerField(required=True, error_messages={
        'required': '请选择货物',
    })
    title = serializers.CharField(min_length=1, max_length=100, required=True, error_messages={
        'required': '请输入调查事项',
        'blank': '调查事项不能为空',
    })
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_goods(self, value):
        if not Goods.objects.filter(pk=value).exists():
            raise serializers.ValidationError('货物不存在')
        return value


class DestructionPlanItemSerializer(serializers.ModelSerializer):
    """销毁计划明细序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = DestructionPlanItem
        fields = [
            'id', 'plan', 'goods', 'goods_name', 'goods_code',
            'quantity', 'reason', 'method', 'remark',
            'status', 'status_display', 'review_note', 'execute_note',
            'destroyed_at', 'created_at', 'updated_at'
        ]


class DestructionPlanSerializer(serializers.ModelSerializer):
    """销毁计划序列化器"""
    items = DestructionPlanItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.username', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.username', read_only=True)
    executed_by_name = serializers.CharField(source='executed_by.username', read_only=True)

    class Meta:
        model = DestructionPlan
        fields = [
            'id', 'plan_no', 'title', 'status', 'status_display', 'remark',
            'created_by', 'created_by_name',
            'reviewed_by', 'reviewed_by_name', 'reviewed_at', 'review_remark',
            'approved_by', 'approved_by_name', 'approved_at', 'approve_remark',
            'executed_by', 'executed_by_name', 'executed_at',
            'items', 'created_at', 'updated_at'
        ]


class DestructionPlanItemInputSerializer(serializers.Serializer):
    """销毁计划明细录入序列化器"""
    goods = serializers.IntegerField(required=True, error_messages={
        'required': '请选择货物',
    })
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=True,
        error_messages={'required': '请输入销毁数量'}
    )
    reason = serializers.CharField(min_length=1, max_length=200, required=True, error_messages={
        'required': '请输入销毁原因',
        'blank': '销毁原因不能为空',
    })
    method = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_goods(self, value):
        try:
            goods = Goods.objects.get(pk=value)
        except Goods.DoesNotExist:
            raise serializers.ValidationError('货物不存在')
        if not goods.is_active:
            raise serializers.ValidationError(f'货物"{goods.name}"已停用')
        if DestructionPlanItem.objects.filter(
            goods_id=value,
            plan__status__in=['planning', 'reviewed', 'approved']
        ).exists():
            raise serializers.ValidationError(f'货物"{goods.name}"已存在于未完成的销毁计划中')
        return value

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError('销毁数量必须大于0')
        return value


class DestructionPlanCreateSerializer(serializers.Serializer):
    """销毁计划创建序列化器"""
    title = serializers.CharField(min_length=1, max_length=100, required=True, error_messages={
        'required': '请输入计划名称',
        'blank': '计划名称不能为空',
    })
    remark = serializers.CharField(required=False, allow_blank=True, default='')
    items = DestructionPlanItemInputSerializer(many=True, required=True, error_messages={
        'required': '请添加销毁明细',
    })

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError('请至少添加一条销毁明细')
        goods_ids = [item['goods'] for item in value]
        if len(goods_ids) != len(set(goods_ids)):
            raise serializers.ValidationError('同一货物在计划中重复出现')
        goods_map = {g.pk: g for g in Goods.objects.filter(pk__in=goods_ids)}
        for item in value:
            goods = goods_map.get(item['goods'])
            if goods and item['quantity'] > goods.quantity:
                raise serializers.ValidationError(
                    f'货物"{goods.name}"销毁数量超过当前库存（{goods.quantity}）'
                )
        return value


class DestructionSnapshotSerializer(serializers.ModelSerializer):
    """销毁阶段清单摘要序列化器"""
    stage_display = serializers.CharField(source='get_stage_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = DestructionStageSnapshot
        fields = [
            'id', 'plan', 'stage', 'stage_display', 'summary', 'digest',
            'created_by', 'created_by_name', 'created_at'
        ]


class DestructionCorrectionSerializer(serializers.ModelSerializer):
    """销毁更正序列化器"""
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    goods_name = serializers.CharField(source='item.goods.name', read_only=True)

    class Meta:
        model = DestructionCorrection
        fields = [
            'id', 'plan', 'item', 'goods_name', 'changes', 'reason',
            'created_by', 'created_by_name', 'created_at'
        ]


class DestructionCorrectionCreateSerializer(serializers.Serializer):
    """销毁更正创建序列化器（仅允许更正元数据字段）"""
    ALLOWED_FIELDS = ('reason', 'method', 'remark')

    item = serializers.IntegerField(required=True, error_messages={
        'required': '请选择销毁明细',
    })
    changes = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=True,
        error_messages={'required': '请提供更正内容'}
    )
    reason = serializers.CharField(min_length=1, max_length=300, required=True, error_messages={
        'required': '请输入更正原因',
        'blank': '更正原因不能为空',
    })

    def validate_changes(self, value):
        if not value:
            raise serializers.ValidationError('更正内容不能为空')
        unknown = [field for field in value if field not in self.ALLOWED_FIELDS]
        if unknown:
            raise serializers.ValidationError(
                f'更正仅允许修改元数据字段（{ "、".join(self.ALLOWED_FIELDS) }），'
                f'不允许修改：{ "、".join(unknown) }；库存不可通过更正恢复'
            )
        return value
