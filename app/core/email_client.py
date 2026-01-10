"""
邮件发送客户端
"""
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from typing import Optional, List
import asyncio
from concurrent.futures import ThreadPoolExecutor
from app.core.config import settings


class EmailClient:
    """邮件发送客户端"""
    
    def __init__(self):
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.user = settings.smtp_user
        self.password = settings.smtp_password
        self.from_name = settings.smtp_from_name
        self._executor = ThreadPoolExecutor(max_workers=2)
    
    def _send_sync(
        self,
        to: str,
        subject: str,
        body: str,
        is_html: bool = True
    ) -> bool:
        """同步发送邮件"""
        if not self.host or not self.user:
            print("Email not configured, skipping...")
            return False
        
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{self.from_name} <{self.user}>"
            msg["To"] = to
            msg["Subject"] = Header(subject, "utf-8")
            
            content_type = "html" if is_html else "plain"
            msg.attach(MIMEText(body, content_type, "utf-8"))
            
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, self.port, context=context) as server:
                server.login(self.user, self.password)
                server.sendmail(self.user, to, msg.as_string())
            
            return True
        except Exception as e:
            print(f"Email send error: {e}")
            return False
    
    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        is_html: bool = True
    ) -> bool:
        """异步发送邮件"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._send_sync,
            to, subject, body, is_html
        )
    
    async def send_activation_email(self, to: str, username: str, token: str, base_url: str) -> bool:
        """发送账号激活邮件"""
        activation_link = f"{base_url}/api/v1/users/activate/{token}"
        
        subject = "【巴特星球】账号激活"
        body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .button {{ display: inline-block; background: #667eea; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
                .footer {{ text-align: center; color: #999; margin-top: 20px; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>欢迎加入巴特星球</h1>
                </div>
                <div class="content">
                    <p>亲爱的 {username}，</p>
                    <p>感谢您注册巴特星球AIGC云平台！请点击下方按钮激活您的账号：</p>
                    <p style="text-align: center;">
                        <a href="{activation_link}" class="button">激活账号</a>
                    </p>
                    <p>或者复制以下链接到浏览器：</p>
                    <p style="word-break: break-all; color: #666;">{activation_link}</p>
                    <p>此链接24小时内有效。</p>
                    <p>如果您没有注册过账号，请忽略此邮件。</p>
                </div>
                <div class="footer">
                    <p>© 2024 巴特星球 - AIGC云平台</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return await self.send(to, subject, body)
    
    async def send_reset_password_email(self, to: str, username: str, token: str, base_url: str) -> bool:
        """发送重置密码邮件"""
        reset_link = f"{base_url}/reset-password?token={token}"
        
        subject = "【巴特星球】重置密码"
        body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .button {{ display: inline-block; background: #f5576c; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
                .footer {{ text-align: center; color: #999; margin-top: 20px; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>重置密码</h1>
                </div>
                <div class="content">
                    <p>亲爱的 {username}，</p>
                    <p>您请求重置密码，请点击下方按钮进行重置：</p>
                    <p style="text-align: center;">
                        <a href="{reset_link}" class="button">重置密码</a>
                    </p>
                    <p>或者复制以下链接到浏览器：</p>
                    <p style="word-break: break-all; color: #666;">{reset_link}</p>
                    <p>此链接1小时内有效。</p>
                    <p>如果您没有请求重置密码，请忽略此邮件。</p>
                </div>
                <div class="footer">
                    <p>© 2024 巴特星球 - AIGC云平台</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return await self.send(to, subject, body)
    
    async def send_product_review_notification(
        self, 
        to: str, 
        username: str, 
        product_name: str, 
        status: str, 
        note: Optional[str] = None
    ) -> bool:
        """发送商品审核结果通知"""
        status_text = "已通过" if status == "approved" else "已拒绝"
        status_color = "#52c41a" if status == "approved" else "#ff4d4f"
        
        subject = f"【巴特星球】商品审核{status_text}"
        body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: {status_color}; color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .status {{ font-size: 24px; font-weight: bold; color: {status_color}; }}
                .footer {{ text-align: center; color: #999; margin-top: 20px; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>商品审核通知</h1>
                </div>
                <div class="content">
                    <p>亲爱的 {username}，</p>
                    <p>您提交的商品 <strong>{product_name}</strong> 审核结果：</p>
                    <p class="status">{status_text}</p>
                    {f'<p>审核备注：{note}</p>' if note else ''}
                    <p>登录平台查看详情。</p>
                </div>
                <div class="footer">
                    <p>© 2024 巴特星球 - AIGC云平台</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return await self.send(to, subject, body)


# 全局单例
email_client = EmailClient()
