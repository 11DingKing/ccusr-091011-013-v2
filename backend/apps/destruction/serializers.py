"""
物资销毁管理序列化器
"""
from rest_framework import serializers

from .models import (
    CORRECTABLE_FIELDS,
    DestructionCorrection,
    DestructionItem,
    DestructionPlan,
    DestructionStageSnapshot,
)


class DestructionPlanCreateSerializer(serializers.Serializer):
    """销毁计划创建"""
    title = serializers.CharField(max_length=200, required=True,
                                  error_messages={'required': '请输入计划标题', 'blank': '计划标题不能为空'})
    goods_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True,
        error_messages={'required': '请选择列入销毁清单的物资', 'empty': '请至少选择一项物资'})
    scheduled_date = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class DestructionReviewSerializer(serializers.Serializer):
    """资格复核提交"""
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class DestructionDecisionItemSerializer(serializers.Serializer):
    approved = serializers.BooleanField(required=True)
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class DestructionApprovalSerializer(serializers.Serializer):
    """批准决定"""
    approved = serializers.BooleanField(required=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')
    # 键为条目 id（字符串或数字），值为 {"approved": bool, "reason": str}
    item_decisions = serializers.DictField(
        child=DestructionDecisionItemSerializer(), required=False, default=dict)


class DestructionExecuteSerializer(serializers.Serializer):
    """执行确认"""
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class DestructionCorrectionSerializer(serializers.Serializer):
    """销毁后元数据更正"""
    changes = serializers.DictField(
        child=serializers.CharField(allow_blank=True, allow_null=True),
        required=True, allow_empty=False,
        error_messages={'required': '请提供更正内容', 'empty': '请提供更正内容'})
    reason = serializers.CharField(required=True, allow_blank=False,
                                  error_messages={'required': '请填写更正原因', 'blank': '更正原因不能为空'})

    def validate_changes(self, value):
        unknown = [f for f in value if f not in CORRECTABLE_FIELDS]
        if unknown:
            raise serializers.ValidationError(
                f'不允许更正的字段：{unknown}；仅允许：{CORRECTABLE_FIELDS}')
        return value


class DestructionItemSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = DestructionItem
        fields = [
            'id', 'goods', 'goods_code', 'goods_name', 'quantity_at_plan',
            'status', 'status_display', 'block_reasons', 'plan_warnings',
            'reviewed_at', 'approved_at', 'destroyed_at', 'created_at',
        ]


class DestructionStageSnapshotSerializer(serializers.ModelSerializer):
    stage_display = serializers.CharField(source='get_stage_display', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)

    class Meta:
        model = DestructionStageSnapshot
        fields = [
            'id', 'stage', 'stage_display', 'summary', 'checksum',
            'operator', 'operator_name', 'note', 'created_at',
        ]


class DestructionCorrectionRecordSerializer(serializers.ModelSerializer):
    field_label = serializers.CharField(read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)

    class Meta:
        model = DestructionCorrection
        fields = [
            'id', 'plan', 'item', 'goods', 'field_name', 'field_label',
            'old_value', 'new_value', 'reason', 'operator', 'operator_name', 'created_at',
        ]


class DestructionPlanListSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    item_count = serializers.IntegerField(read_only=True)
    destroyed_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = DestructionPlan
        fields = [
            'id', 'plan_no', 'title', 'scheduled_date', 'reason',
            'status', 'status_display', 'item_count', 'destroyed_count',
            'created_by', 'created_by_name',
            'reviewed_by', 'approved_by', 'executed_by',
            'reviewed_at', 'approved_at', 'executed_at',
            'created_at', 'updated_at',
        ]


class DestructionPlanDetailSerializer(DestructionPlanListSerializer):
    items = DestructionItemSerializer(many=True, read_only=True)
    snapshots = DestructionStageSnapshotSerializer(many=True, read_only=True)
    corrections = DestructionCorrectionRecordSerializer(many=True, read_only=True)

    class Meta(DestructionPlanListSerializer.Meta):
        fields = DestructionPlanListSerializer.Meta.fields + [
            'review_remark', 'approval_remark', 'execute_remark',
            'items', 'snapshots', 'corrections',
        ]
