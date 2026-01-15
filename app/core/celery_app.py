"""
Celery 配置
"""
from celery import Celery
from app.core.config import settings

# 创建 Celery 应用
celery_app = Celery(
    "aigccloud",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.payment_tasks",
        "app.tasks.ai_tasks",
    ]
)

# Celery 配置
celery_app.conf.update(
    # 任务序列化
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,
    
    # 任务结果过期时间（1天）
    result_expires=86400,
    
    # 任务确认（任务完成后才确认）
    task_acks_late=True,
    
    # 预取数量（每个 worker 一次取一个任务）
    worker_prefetch_multiplier=1,
    
    # 任务超时
    task_soft_time_limit=300,  # 5分钟软超时
    task_time_limit=600,       # 10分钟硬超时
    
    # 定时任务
    beat_schedule={
        # 每5分钟检查待处理订单
        "process-pending-orders": {
            "task": "app.tasks.payment_tasks.process_pending_orders",
            "schedule": 300.0,  # 5分钟
        },
        # 每10分钟检查超时任务
        "check-timeout-tasks": {
            "task": "app.tasks.ai_tasks.check_timeout_tasks",
            "schedule": 600.0,  # 10分钟
        },
    },
)
