import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional, List
from ...core.config.config import settings
import logging

logger = logging.getLogger(__name__)


class EmailSender:

    def __init__(self):
        self.smtp_host = os.environ.get('SMTP_HOST', settings.email_settings.get('smtp_host', ''))
        self.smtp_port = int(os.environ.get('SMTP_PORT', settings.email_settings.get('smtp_port', 465)))
        self.smtp_user = os.environ.get('SMTP_USER', settings.email_settings.get('smtp_user', ''))
        self.smtp_password = os.environ.get('SMTP_PASSWORD', settings.email_settings.get('smtp_password', ''))
        self.sender = os.environ.get('EMAIL_SENDER', settings.email_settings.get('sender', ''))
        self.recipients = os.environ.get('EMAIL_RECIPIENTS', settings.email_settings.get('recipients', '')).split(',')
        self.use_ssl = settings.email_settings.get('use_ssl', True)

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.sender and self.recipients[0])

    def send_newsletter(self, html_content: str, subject: str = None, date_str: str = None) -> bool:
        if not self.is_configured():
            logger.warning("邮件推送未配置，跳过发送")
            return False

        if not subject:
            if not date_str:
                from datetime import datetime, timezone, timedelta
                tz = timezone(timedelta(hours=8))
                date_str = datetime.now(tz).strftime('%Y-%m-%d')
            subject = f"每日新闻简报 - {date_str}"

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender
            msg['To'] = ', '.join(self.recipients)

            html_part = MIMEText(html_content, 'html', 'utf-8')
            msg.attach(html_part)

            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
                server.starttls()

            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.sender, self.recipients, msg.as_string())
            server.quit()

            logger.info(f"邮件已发送至: {', '.join(self.recipients)}")
            return True

        except Exception as e:
            logger.error(f"发送邮件时出错: {str(e)}")
            return False

    def send_test(self) -> bool:
        if not self.is_configured():
            return False

        html = """
        <html><body>
        <h2>邮件推送测试</h2>
        <p>如果您看到这封邮件，说明邮件推送功能配置成功！</p>
        <p>来自每日新闻流系统</p>
        </body></html>
        """
        return self.send_newsletter(html, subject="每日新闻流 - 邮件测试")
