"""Email notifications: urgent events are sent right away, the rest in a daily digest."""

import html
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

from .settings import EmailSettings

KIND_LABELS = {
    "added": "手动添加",
    "candidate": "新的待确认影片",
    "dated": "已定档",
    "streaming": "已上线",
    "downloaded": "已开始下载",
    "download_failed": "下载失败",
    "site_invalid": "站点登录失效",
}


class NotifyError(Exception):
    pass


def send_mail(config: EmailSettings, subject: str, body_html: str) -> None:
    host, port, security = config.resolved_server()
    if not host or not config.username:
        raise NotifyError("邮件未配置完整")
    recipients = config.recipients or [config.username]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("ReelReady", config.username))
    msg["To"] = ", ".join(recipients)
    msg.set_content("请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(body_html, subtype="html")

    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(config.username, config.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(config.username, config.password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError("邮箱登录失败，请检查账号和授权码") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"发送邮件失败: {exc}") from exc


def _layout(heading: str, inner: str) -> str:
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f4f4f7;font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#1f2330">
<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.06)">
  <div style="padding:20px 24px;background:#16181f;color:#fff">
    <div style="font-size:13px;letter-spacing:.08em;color:#f5b84b;font-weight:600">REELREADY</div>
    <div style="font-size:20px;font-weight:600;margin-top:4px">{html.escape(heading)}</div>
  </div>
  <div style="padding:8px 24px 24px">{inner}</div>
</div>
<div style="text-align:center;color:#9a9caa;font-size:12px;margin-top:16px">由 ReelReady 自动发送</div>
</body></html>"""


def _item(title: str, message: str, when: datetime | None = None) -> str:
    time_html = (
        f'<div style="color:#9a9caa;font-size:12px;margin-top:4px">{when:%Y-%m-%d %H:%M}</div>' if when else ""
    )
    message_html = html.escape(message).replace("\n", "<br>")
    return f"""<div style="padding:14px 0;border-bottom:1px solid #eee">
  <div style="font-size:15px;font-weight:600">{html.escape(title)}</div>
  <div style="font-size:14px;color:#4a4d5a;margin-top:4px;line-height:1.6">{message_html}</div>{time_html}
</div>"""


def render_single(title: str, message: str) -> str:
    return _layout(title, _item(title, message))


def render_digest(groups: dict[str, list[tuple[str, str, datetime]]]) -> str:
    sections = []
    for kind, items in groups.items():
        label = KIND_LABELS.get(kind, kind)
        rows = "".join(_item(title, message, when) for title, message, when in items)
        sections.append(
            f'<h3 style="font-size:14px;color:#c2851f;margin:20px 0 0">{html.escape(label)} · {len(items)}</h3>{rows}'
        )
    return _layout("每日汇总", "".join(sections))
