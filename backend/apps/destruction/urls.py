"""
物资销毁管理 URL 配置
"""
from django.urls import path

from .views import (
    DestructionApprovalView,
    DestructionCorrectionView,
    DestructionEligibilityPrecheckView,
    DestructionExecuteView,
    DestructionPlanDetailView,
    DestructionPlanListView,
    DestructionReviewView,
    DestructionSnapshotListView,
)

urlpatterns = [
    # 销毁计划（阶段一）
    path('destruction/plans/', DestructionPlanListView.as_view(),
         name='destruction-plan-list'),
    path('destruction/plans/<int:pk>/', DestructionPlanDetailView.as_view(),
         name='destruction-plan-detail'),

    # 列入前资格预检（冻结 / 未结调查 / 未结领用 / 保管期限）
    path('destruction/eligibility-precheck/',
         DestructionEligibilityPrecheckView.as_view(),
         name='destruction-eligibility-precheck'),

    # 阶段二：资格复核
    path('destruction/plans/<int:pk>/review/', DestructionReviewView.as_view(),
         name='destruction-plan-review'),

    # 阶段三：批准
    path('destruction/plans/<int:pk>/approval/', DestructionApprovalView.as_view(),
         name='destruction-plan-approval'),

    # 阶段四：执行确认（不可逆）
    path('destruction/plans/<int:pk>/execute/', DestructionExecuteView.as_view(),
         name='destruction-plan-execute'),

    # 各阶段清单摘要
    path('destruction/plans/<int:pk>/snapshots/', DestructionSnapshotListView.as_view(),
         name='destruction-plan-snapshots'),

    # 销毁后元数据更正
    path('destruction/items/<int:item_id>/corrections/',
         DestructionCorrectionView.as_view(),
         name='destruction-item-corrections'),
]
