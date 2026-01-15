"""
支付相关异步任务
"""
import asyncio
from app.core.celery_app import celery_app
from app.core.logger import logger
from app.core.parse_client import parse_client
from app.core.wechat_pay import wechat_pay


def run_async(coro):
    """在 Celery 同步上下文中运行异步代码"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=3)
def process_pending_orders(self):
    """
    处理待支付订单
    - 查询微信支付状态
    - 更新订单状态
    """
    logger.info("[Celery] 开始处理待支付订单...")
    
    async def _process():
        try:
            # 查询待处理订单
            result = await parse_client.query_objects(
                "Order",
                where={"status": "pending"},
                limit=50,
            )
            orders = result.get("results", [])
            
            if not orders:
                logger.info("[Celery] 无待处理订单")
                return {"processed": 0}
            
            processed = 0
            for order in orders:
                order_id = order.get("orderId")
                try:
                    # 查询微信支付状态
                    pay_result = await wechat_pay.query_order(order_id)
                    
                    if pay_result.get("trade_state") == "SUCCESS":
                        # 更新订单状态
                        await parse_client.update_object(
                            "Order",
                            order["objectId"],
                            {"status": "paid", "paidAt": {"__type": "Date", "iso": pay_result.get("time_end")}}
                        )
                        logger.info(f"[Celery] 订单 {order_id} 支付成功")
                        processed += 1
                        
                        # 触发后续任务（充值金币等）
                        process_paid_order.delay(order["objectId"])
                        
                except Exception as e:
                    logger.error(f"[Celery] 处理订单 {order_id} 失败: {e}")
                    
            return {"processed": processed, "total": len(orders)}
            
        except Exception as e:
            logger.error(f"[Celery] 处理待支付订单失败: {e}")
            raise
    
    return run_async(_process())


@celery_app.task(bind=True, max_retries=3)
def process_paid_order(self, order_object_id: str):
    """
    处理已支付订单
    - 充值金币
    - 更新会员状态
    - 发放奖励
    """
    logger.info(f"[Celery] 处理已支付订单: {order_object_id}")
    
    async def _process():
        try:
            # 获取订单详情
            order = await parse_client.get_object("Order", order_object_id)
            if not order:
                logger.error(f"[Celery] 订单不存在: {order_object_id}")
                return {"success": False, "error": "订单不存在"}
            
            order_type = order.get("type")
            user_id = order.get("userId")
            
            if order_type == "recharge":
                # 充值金币
                coins = order.get("coins", 0)
                if coins > 0:
                    from app.core.incentive_service import incentive_service, IncentiveType
                    await incentive_service.reward_user(
                        user_id=user_id,
                        reward_type=IncentiveType.RECHARGE,
                        amount=coins,
                        description=f"充值 {coins} 金币"
                    )
                    logger.info(f"[Celery] 用户 {user_id} 充值 {coins} 金币成功")
                    
            elif order_type == "subscription":
                # 会员订阅处理
                # 调用 member 模块的完成订单逻辑
                from app.api.v1.endpoints.member import complete_member_order
                await complete_member_order(order.get("orderId"), order)
                
            return {"success": True}
            
        except Exception as e:
            logger.error(f"[Celery] 处理已支付订单失败: {e}")
            raise self.retry(exc=e, countdown=60)  # 1分钟后重试
    
    return run_async(_process())


@celery_app.task
def handle_wechat_callback(xml_data: str):
    """
    处理微信支付回调
    """
    logger.info("[Celery] 处理微信支付回调")
    
    async def _handle():
        try:
            # 解析回调数据
            result = await wechat_pay.parse_callback(xml_data)
            if not result:
                return {"success": False, "error": "解析回调失败"}
            
            order_id = result.get("out_trade_no")
            
            # 查询订单
            orders = await parse_client.query_objects(
                "Order",
                where={"orderId": order_id},
                limit=1
            )
            orders_list = orders.get("results", [])
            
            if not orders_list:
                logger.error(f"[Celery] 回调订单不存在: {order_id}")
                return {"success": False, "error": "订单不存在"}
            
            order = orders_list[0]
            
            if order.get("status") == "paid":
                logger.info(f"[Celery] 订单 {order_id} 已处理")
                return {"success": True, "message": "已处理"}
            
            # 更新订单状态
            await parse_client.update_object(
                "Order",
                order["objectId"],
                {"status": "paid"}
            )
            
            # 触发后续处理
            process_paid_order.delay(order["objectId"])
            
            return {"success": True}
            
        except Exception as e:
            logger.error(f"[Celery] 处理微信回调失败: {e}")
            raise
    
    return run_async(_handle())
