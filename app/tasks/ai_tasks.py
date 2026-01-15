"""
AI 任务相关异步任务
"""
import asyncio
from datetime import datetime, timedelta
from app.core.celery_app import celery_app
from app.core.logger import logger
from app.core.parse_client import parse_client


def run_async(coro):
    """在 Celery 同步上下文中运行异步代码"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=3)
def execute_ai_task(self, task_id: str, task_type: str, params: dict):
    """
    执行 AI 任务
    """
    logger.info(f"[Celery] 执行 AI 任务: {task_id}, 类型: {task_type}")
    
    async def _execute():
        try:
            # 更新任务状态为处理中
            await parse_client.update_object(
                "AITask",
                task_id,
                {
                    "status": "processing",
                    "startedAt": {"__type": "Date", "iso": datetime.utcnow().isoformat() + "Z"}
                }
            )
            
            # 根据任务类型执行不同逻辑
            result = None
            if task_type == "image_generation":
                result = await _generate_image(params)
            elif task_type == "text_generation":
                result = await _generate_text(params)
            elif task_type == "video_generation":
                result = await _generate_video(params)
            else:
                raise ValueError(f"未知任务类型: {task_type}")
            
            # 更新任务状态为完成
            await parse_client.update_object(
                "AITask",
                task_id,
                {
                    "status": "completed",
                    "result": result,
                    "completedAt": {"__type": "Date", "iso": datetime.utcnow().isoformat() + "Z"}
                }
            )
            
            logger.info(f"[Celery] AI 任务完成: {task_id}")
            return {"success": True, "result": result}
            
        except Exception as e:
            logger.error(f"[Celery] AI 任务失败: {task_id}, 错误: {e}")
            
            # 更新任务状态为失败
            await parse_client.update_object(
                "AITask",
                task_id,
                {
                    "status": "failed",
                    "error": str(e),
                    "completedAt": {"__type": "Date", "iso": datetime.utcnow().isoformat() + "Z"}
                }
            )
            
            raise self.retry(exc=e, countdown=30)  # 30秒后重试
    
    return run_async(_execute())


async def _generate_image(params: dict) -> dict:
    """图像生成（模拟）"""
    await asyncio.sleep(2)  # 模拟处理时间
    return {
        "type": "image",
        "url": f"https://placeholder.com/generated_{params.get('seed', 0)}.png",
        "width": params.get("width", 512),
        "height": params.get("height", 512),
    }


async def _generate_text(params: dict) -> dict:
    """文本生成（模拟）"""
    await asyncio.sleep(1)
    return {
        "type": "text",
        "content": f"Generated text based on: {params.get('prompt', '')}",
    }


async def _generate_video(params: dict) -> dict:
    """视频生成（模拟）"""
    await asyncio.sleep(5)
    return {
        "type": "video",
        "url": f"https://placeholder.com/generated_video.mp4",
        "duration": params.get("duration", 10),
    }


@celery_app.task
def check_timeout_tasks():
    """
    检查超时任务
    - 将超过30分钟未完成的任务标记为超时
    """
    logger.info("[Celery] 检查超时任务...")
    
    async def _check():
        try:
            timeout_threshold = datetime.utcnow() - timedelta(minutes=30)
            
            # 查询处理中但超时的任务
            result = await parse_client.query_objects(
                "AITask",
                where={
                    "status": "processing",
                    "startedAt": {
                        "$lt": {"__type": "Date", "iso": timeout_threshold.isoformat() + "Z"}
                    }
                },
                limit=100
            )
            tasks = result.get("results", [])
            
            if not tasks:
                logger.info("[Celery] 无超时任务")
                return {"timeout_count": 0}
            
            for task in tasks:
                await parse_client.update_object(
                    "AITask",
                    task["objectId"],
                    {
                        "status": "timeout",
                        "error": "任务处理超时",
                        "completedAt": {"__type": "Date", "iso": datetime.utcnow().isoformat() + "Z"}
                    }
                )
                logger.warning(f"[Celery] 任务超时: {task['objectId']}")
            
            return {"timeout_count": len(tasks)}
            
        except Exception as e:
            logger.error(f"[Celery] 检查超时任务失败: {e}")
            raise
    
    return run_async(_check())
