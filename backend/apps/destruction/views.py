"""
物资销毁管理视图

四阶段：销毁计划 → 资格复核 → 批准 → 执行确认；
执行后可提交只改元数据的更正。
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import success_response, error_response
from apps.warehouse.models import Goods

from .models import (
    DestructionItem,
    DestructionPlan,
)
from .serializers import (
    DestructionApprovalSerializer,
    DestructionCorrectionRecordSerializer,
    DestructionCorrectionSerializer,
    DestructionPlanCreateSerializer,
    DestructionPlanDetailSerializer,
    DestructionPlanListSerializer,
    DestructionReviewSerializer,
    DestructionExecuteSerializer,
    DestructionStageSnapshotSerializer,
)
from .services import (
    DestructionError,
    check_eligibility,
    confirm_execution,
    correct_metadata,
    create_plan,
    decide_plan,
    submit_review,
)

logger = logging.getLogger('apps')


def _parse_errors(serializer):
    errors = serializer.errors
    first_key = next(iter(errors))
    first_error = errors[first_key]
    if isinstance(first_error, list):
        first_error = first_error[0]
    if isinstance(first_error, dict):
        first_error = str(first_error)
    return str(first_error)


class DestructionPlanListView(APIView):
    """销毁计划列表 / 建立销毁计划（阶段一）"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = DestructionPlan.objects.all().order_by('-created_at')

        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        keyword = request.query_params.get('keyword')
        if keyword:
            queryset = queryset.filter(plan_no__icontains=keyword) | \
                queryset.filter(title__icontains=keyword)

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = max(int(request.query_params.get('page_size', 10)), 1)
        except (TypeError, ValueError):
            page, page_size = 1, 10

        total = queryset.count()
        plans = queryset[(page - 1) * page_size:page * page_size]
        return success_response(data={
            'list': DestructionPlanListSerializer(plans, many=True).data,
            'total': total,
            'page': page,
            'page_size': page_size,
        })

    def post(self, request):
        serializer = DestructionPlanCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_parse_errors(serializer))

        data = serializer.validated_data
        try:
            plan, snapshot, rejected = create_plan(
                operator=request.user,
                title=data['title'],
                goods_ids=data['goods_ids'],
                scheduled_date=data.get('scheduled_date'),
                reason=data.get('reason', ''),
            )
        except DestructionError as exc:
            return error_response(message=str(exc))

        logger.info("用户 %s 建立销毁计划 %s（%d 项）",
                    request.user.username, plan.plan_no, plan.item_count)
        return success_response(
            data={
                'plan': DestructionPlanDetailSerializer(plan).data,
                'snapshot_id': snapshot.id,
                'rejected': rejected,
            },
            message='销毁计划创建成功')


class DestructionPlanDetailView(APIView):
    """销毁计划详情（含各条目与全部阶段快照）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        plan = DestructionPlan.objects.prefetch_related(
            'items', 'snapshots__operator', 'corrections').filter(pk=pk).first()
        if plan is None:
            return error_response(message='销毁计划不存在', code=404)
        return success_response(data=DestructionPlanDetailSerializer(plan).data)


class DestructionEligibilityPrecheckView(APIView):
    """列入前资格预检：冻结、未结调查、未结领用、保管期限等"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        goods_ids = request.data.get('goods_ids', [])
        if not goods_ids:
            return error_response(message='请选择需要预检的物资')

        goods_list = Goods.objects.filter(pk__in=goods_ids)
        result = []
        for goods in goods_list:
            passed, reasons = check_eligibility(goods)
            result.append({
                'goods_id': goods.id,
                'code': goods.code,
                'name': goods.name,
                'eligible': passed,
                'reasons': reasons,
                'is_frozen': goods.is_frozen,
                'has_open_investigation': goods.has_open_investigation,
                'has_open_stock_out': goods.has_open_stock_out,
                'retention_expire_date': goods.retention_expire_date,
                'lifecycle_status': goods.lifecycle_status,
            })
        return success_response(data={'results': result})


class DestructionReviewView(APIView):
    """资格复核（阶段二）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = DestructionReviewSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_parse_errors(serializer))

        try:
            plan, snapshot, excluded = submit_review(
                plan_id=pk, operator=request.user,
                remark=serializer.validated_data.get('remark', ''))
        except DestructionError as exc:
            return error_response(message=str(exc))

        logger.info("用户 %s 完成销毁计划 %s 资格复核，剔除 %d 项",
                    request.user.username, plan.plan_no, len(excluded))
        return success_response(data={
            'plan': DestructionPlanDetailSerializer(plan).data,
            'snapshot_id': snapshot.id,
            'excluded': excluded,
        }, message='资格复核完成')


class DestructionApprovalView(APIView):
    """批准（阶段三）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = DestructionApprovalSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_parse_errors(serializer))

        data = serializer.validated_data
        try:
            plan, snapshot, rejected = decide_plan(
                plan_id=pk, operator=request.user,
                approved=data['approved'],
                item_decisions=data.get('item_decisions') or {},
                remark=data.get('remark', ''))
        except DestructionError as exc:
            return error_response(message=str(exc))

        logger.info("用户 %s 对销毁计划 %s 作出批准决定（%s）",
                    request.user.username, plan.plan_no, plan.get_status_display())
        return success_response(data={
            'plan': DestructionPlanDetailSerializer(plan).data,
            'snapshot_id': snapshot.id,
            'rejected_item_ids': rejected,
        }, message='批准决定已记录')


class DestructionExecuteView(APIView):
    """执行确认（阶段四，不可逆）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = DestructionExecuteSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_parse_errors(serializer))

        try:
            plan, snapshot, blocked, destroyed = confirm_execution(
                plan_id=pk, operator=request.user,
                remark=serializer.validated_data.get('remark', ''))
        except DestructionError as exc:
            return error_response(message=str(exc))

        logger.warning(
            "用户 %s 执行确认销毁计划 %s：销毁 %d 项，拦截 %d 项",
            request.user.username, plan.plan_no, len(destroyed), len(blocked))
        return success_response(data={
            'plan': DestructionPlanDetailSerializer(plan).data,
            'snapshot_id': snapshot.id,
            'destroyed_item_ids': destroyed,
            'blocked': blocked,
        }, message=f'执行确认完成：销毁 {len(destroyed)} 项，拦截 {len(blocked)} 项')


class DestructionSnapshotListView(APIView):
    """查看某计划各阶段保存的清单摘要"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        plan = DestructionPlan.objects.filter(pk=pk).first()
        if plan is None:
            return error_response(message='销毁计划不存在', code=404)
        snapshots = plan.snapshots.all()
        return success_response(data={
            'list': DestructionStageSnapshotSerializer(snapshots, many=True).data,
            'total': snapshots.count(),
        })


class DestructionCorrectionView(APIView):
    """销毁后元数据更正（不恢复库存、不改变终态）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, item_id):
        serializer = DestructionCorrectionSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_parse_errors(serializer))

        try:
            item, records = correct_metadata(
                item_id=item_id, operator=request.user,
                changes=serializer.validated_data['changes'],
                reason=serializer.validated_data['reason'])
        except DestructionError as exc:
            return error_response(message=str(exc))

        logger.warning(
            "用户 %s 对销毁条目 %s 提交 %d 项元数据更正（计划 %s）",
            request.user.username, item_id, len(records), item.plan.plan_no)
        return success_response(data={
            'item_id': item.id,
            'records': DestructionCorrectionRecordSerializer(records, many=True).data,
        }, message=f'已更正 {len(records)} 个字段（库存与终态未变更）')

    def get(self, request, item_id):
        item = DestructionItem.objects.filter(pk=item_id).first()
        if item is None:
            return error_response(message='销毁条目不存在', code=404)
        records = item.corrections.all()
        return success_response(data={
            'list': DestructionCorrectionRecordSerializer(records, many=True).data,
            'total': records.count(),
        })
