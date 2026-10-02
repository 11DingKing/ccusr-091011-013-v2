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

## 销毁流程

保管期限届满的物资通过四阶段流程销毁，每个阶段留存当时的清单摘要（含 SHA-256 摘要值）：

1. **销毁计划** `POST /api/destruction-plans/` — 提交销毁明细（货物、数量、原因）
2. **资格复核** `POST /api/destruction-plans/{id}/review/` — 逐项核查保管期限、冻结、未结调查、领用状态与库存，不合规项标记为复核不通过
3. **批准** `POST /api/destruction-plans/{id}/approve/` — `decision: approved/rejected`
4. **执行确认** `POST /api/destruction-plans/{id}/execute/` — 执行前再次核查冻结、未结调查与领用状态；每条明细在独立事务中处理，单项失败不影响其他项，每项都有明确结果（已销毁/执行拦截/处理失败）。执行后物资进入不可逆终态

执行后如发现录入错误，可提交仅修改元数据、不恢复库存的更正：
`POST /api/destruction-plans/{id}/corrections/`（仅允许 `reason`、`method`、`remark` 字段）。

配套接口：`POST /api/goods/{id}/freeze/`（冻结/解冻）、`POST /api/investigations/`（调查立案）、`POST /api/investigations/{id}/close/`（结案）。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
