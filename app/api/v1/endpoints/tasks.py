"""
AI任务管理端点
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime

from app.core.parse_client import parse_client
from app.core.web3_client import web3_client
from app.core.security import generate_task_id
from app.core.deps import get_current_user_id

router = APIRouter()


# ============ 枚举与模型 ============

class TaskType(str, Enum):
    TXT2IMG = "txt2img"
    IMG2IMG = "img2img"
    TXT2SPEECH = "txt2speech"
    SPEECH2TXT = "speech2txt"
    TXT2MUSIC = "txt2music"
    TXT2VIDEO = "txt2video"


class TaskStatus(int, Enum):
    PENDING = 0  # 排队中
    PROCESSING = 1  # 处理中
    COMPLETED = 2  # 完成
    FAILED = 3  # 失败
    REWARDED = 4  # 已发放奖励


class SubmitTaskRequest(BaseModel):
    type: TaskType
    model: str
    data: Dict[str, Any]


class TaskResult(BaseModel):
    CID: Optional[str] = None
    url: str
    thumbnail: Optional[str] = None


class TaskResponse(BaseModel):
    task_id: str
    type: TaskType
    model: str
    status: TaskStatus
    results: Optional[List[TaskResult]] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class UpdateTaskStatusRequest(BaseModel):
    status: TaskStatus
    results: Optional[List[TaskResult]] = None
    error_message: Optional[str] = None


# ============ 后台任务处理 ============

async def process_ai_task(task_id: str, task_type: str, model: str, data: Dict[str, Any]):
    """
    后台处理AI任务
    TODO: 实际对接AI服务(ComfyUI/Stable Diffusion等)
    """
    try:
        # 更新状态为处理中
        await parse_client.update_object("AITask", task_id, {
            "status": TaskStatus.PROCESSING,
            "updatedAt": datetime.now().isoformat()
        })
        
        # TODO: 根据任务类型调用不同的AI服务
        # 这里是模拟处理
        import asyncio
        await asyncio.sleep(2)  # 模拟处理时间
        
        # 模拟生成结果
        result_url = f"https://storage.example.com/results/{task_id}.png"
        
        # 更新任务结果
        await parse_client.update_object("AITask", task_id, {
            "status": TaskStatus.COMPLETED,
            "results": [{
                "url": result_url,
                "thumbnail": result_url,
            }],
            "updatedAt": datetime.now().isoformat()
        })
        
    except Exception as e:
        # 任务失败
        await parse_client.update_object("AITask", task_id, {
            "status": TaskStatus.FAILED,
            "errorMessage": str(e),
            "updatedAt": datetime.now().isoformat()
        })


# ============ 端点 ============

@router.post("/submit", response_model=TaskResponse)
async def submit_task(
    request: SubmitTaskRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id)
):
    """
    提交AI生成任务
    """
    # 1. 验证用户状态
    try:
        user = await parse_client.get_user(user_id)
    except Exception:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 2. 检查用户余额或会员状态
    is_paid = user.get("isPaid", False)
    balance = user.get("totalIncentive", 0)
    
    # 任务消耗配置
    task_costs = {
        "txt2img": 10,
        "img2img": 15,
        "txt2speech": 5,
        "speech2txt": 5,
        "txt2music": 20,
        "txt2video": 50,
    }
    
    cost = task_costs.get(request.type, 10)
    
    # 付费用户免费，普通用户扣费
    if not is_paid:
        if balance < cost:
            raise HTTPException(status_code=400, detail=f"余额不足，需要 {cost} 金币")
        # 扣除金币
        await parse_client.update_user(user_id, {
            "totalIncentive": parse_client.increment(-cost)
        })
    
    # 3. 生成任务ID
    task_id = generate_task_id()
    
    # 4. 创建任务记录
    task_data = {
        "taskId": task_id,
        "designer": user_id,
        "executor": None,  # Worker 接取任务时填入 Web3 地址
        "type": request.type,
        "model": request.model,
        "data": request.data,
        "status": TaskStatus.PENDING,
        "cost": cost if not is_paid else 0,
    }
    
    result = await parse_client.create_object("AITask", task_data)
    
    # 5. 加入后台处理队列
    background_tasks.add_task(
        process_ai_task,
        result["objectId"],
        request.type,
        request.model,
        request.data
    )
    
    return TaskResponse(
        task_id=task_id,
        type=request.type,
        model=request.model,
        status=TaskStatus.PENDING,
        created_at=datetime.now(),
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, user_id: str = Depends(get_current_user_id)):
    """
    获取任务状态
    """
    # 查询任务
    tasks = await parse_client.query_objects("AITask", where={"taskId": task_id})
    
    if not tasks.get("results"):
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task = tasks["results"][0]
    
    # 验证任务归属
    if task.get("designer") != user_id:
        raise HTTPException(status_code=403, detail="无权访问此任务")
    
    results = None
    if task.get("results"):
        results = [TaskResult(**r) for r in task["results"]]
    
    return TaskResponse(
        task_id=task["taskId"],
        type=task["type"],
        model=task["model"],
        status=task["status"],
        results=results,
        created_at=datetime.fromisoformat(task["createdAt"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(task["updatedAt"].replace("Z", "+00:00")) if task.get("updatedAt") else None,
    )


@router.get("/user/list")
async def get_user_tasks(
    page: int = 1,
    limit: int = 20,
    type: Optional[str] = None,
    status: Optional[int] = None,
    user_id: str = Depends(get_current_user_id)
):
    """
    获取用户的任务列表
    """
    where = {"designer": user_id}
    if type:
        where["type"] = type
    if status is not None:
        where["status"] = status
    
    skip = (page - 1) * limit
    
    result = await parse_client.query_objects(
        "AITask",
        where=where,
        order="-createdAt",
        limit=limit,
        skip=skip
    )
    
    total = await parse_client.count_objects("AITask", where)
    
    tasks = []
    for task in result.get("results", []):
        tasks.append({
            "task_id": task["taskId"],
            "type": task["type"],
            "model": task["model"],
            "status": task["status"],
            "results": task.get("results"),
            "created_at": task["createdAt"],
            "updated_at": task.get("updatedAt"),
        })
    
    return {
        "data": tasks,
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.post("/{task_object_id}/update-status")
async def update_task_status(
    task_object_id: str,
    request: UpdateTaskStatusRequest
):
    """
    更新任务状态(内部调用/Worker回调)
    """
    # 获取任务
    try:
        task = await parse_client.get_object("AITask", task_object_id)
    except Exception:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    update_data = {
        "status": request.status,
        "updatedAt": datetime.now().isoformat()
    }
    
    if request.results:
        update_data["results"] = [r.model_dump() for r in request.results]
    
    if request.error_message:
        update_data["errorMessage"] = request.error_message
    
    # 更新任务
    await parse_client.update_object("AITask", task_object_id, update_data)
    
    # 如果任务完成，发放任务完成奖励
    if request.status == TaskStatus.COMPLETED:
        user_id = task.get("designer")
        if user_id:
            # 获取用户 Web3 地址
            try:
                user = await parse_client.get_user(user_id)
                web3_address = user.get("web3Address")
                reward_amount = 1  # 任务完成奖励1金币
                
                if web3_address:
                    # 通过 Web3 接口发放金币
                    mint_result = await web3_client.mint(web3_address, reward_amount)
                    await parse_client.create_object("IncentiveLog", {
                        "userId": user_id,
                        "web3Address": web3_address,
                        "type": "task",
                        "amount": reward_amount,
                        "txHash": mint_result.get("tx_hash"),
                        "description": f"完成{task['type']}任务奖励"
                    })
            except Exception as e:
                print(f"发放任务奖励失败: {e}")
    
    return {
        "success": True,
        "task_id": task.get("taskId"),
        "status": request.status,
    }


@router.delete("/{task_id}")
async def cancel_task(task_id: str, user_id: str = Depends(get_current_user_id)):
    """
    取消任务(仅排队中的任务可取消)
    """
    # 查询任务
    tasks = await parse_client.query_objects("AITask", where={"taskId": task_id})
    
    if not tasks.get("results"):
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task = tasks["results"][0]
    
    # 验证任务归属
    if task.get("designer") != user_id:
        raise HTTPException(status_code=403, detail="无权操作此任务")
    
    # 只有排队中的任务可以取消
    if task.get("status") != TaskStatus.PENDING:
        raise HTTPException(status_code=400, detail="只有排队中的任务可以取消")
    
    # 退还金币
    cost = task.get("cost", 0)
    if cost > 0:
        await parse_client.update_user(user_id, {
            "totalIncentive": parse_client.increment(cost)
        })
    
    # 删除任务
    await parse_client.delete_object("AITask", task["objectId"])
    
    return {
        "success": True,
        "message": "任务已取消",
        "refund": cost
    }
