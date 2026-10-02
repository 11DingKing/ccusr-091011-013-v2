# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 物资销毁流程

销毁不再依赖线下表格，系统内置四阶段受控流程，每阶段都保存当时的清单摘要：

```
销毁计划 planned → 资格复核 reviewed → 批准 approved → 执行确认 executed（不可逆终态）
                                              └─ 整批驳回 → abandoned（终态）
```

前置状态（`warehouse.Goods` / `warehouse.GoodsHold`）：

- 物资登记 `retention_expire_date`（保管期限届满日）与 `lifecycle_status`（在库/已销毁）。
- 通过 `/api/goods-holds/` 发起、`/api/goods-holds/<id>/release/` 解除**冻结**与**未结调查**；
  **未结领用**自动取未完成的出库审批记录（待审批/已批准）。

接口（均需登录）：

| 阶段 | 接口 |
| --- | --- |
| 列入前预检 | `POST /api/destruction/eligibility-precheck/` |
| ① 建立销毁计划 | `POST /api/destruction/plans/`（重复物资去重；已销毁/已在进行中计划内的物资拒绝列入；冻结等仅作风险提示随项记录） |
| 计划列表/详情 | `GET /api/destruction/plans/`、`GET /api/destruction/plans/<id>/` |
| ② 资格复核 | `POST /api/destruction/plans/<id>/review/`（逐项重检：冻结、未结调查、未结领用、保管期限、库存；不合格项置为“剔除”） |
| ③ 批准 | `POST /api/destruction/plans/<id>/approval/`（可逐项拒绝；整批驳回终止计划） |
| ④ 执行确认 | `POST /api/destruction/plans/<id>/execute/`（执行前再次检查冻结、未结调查、未结领用；通过项清零库存并进入不可逆终态，未通过项置为“拦截”且库存原样保留） |
| 阶段清单摘要 | `GET /api/destruction/plans/<id>/snapshots/` |
| 销毁后元数据更正 | `POST /api/destruction/items/<item_id>/corrections/`（仅可改名称/编码/规格/位置/备注，**不恢复库存、不改变终态**，逐字段留痕） |

关键规则：

- 每个阶段落库一份只追加的清单摘要（含逐项状态、数量汇总、拦截分类与 SHA-256 校验值）。
- 批量处理按条目用数据库保存点隔离：某项失败只把该项标记为剔除/拒绝/拦截并记录原因，其余项照常推进，不存在含糊状态。
- 全部项被拦截时计划停留在“已批准”，不产生任何销毁；管控解除后可再次执行确认。

