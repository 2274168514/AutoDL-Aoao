"""Send GPU availability emails over verified TLS using the standard library."""

from __future__ import annotations

import math
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import Observation
    from .config import EmailSettings


class MailError(Exception):
    """A safe, user-facing mail failure without credentials or server text."""


class EmailNotifier:
    """Send one batch per call; the monitor controls retries and cooldowns.

    Construction never connects to a server. If a send fails or only some
    recipients accept the message, raise MailError so the monitor can retry.
    A later retry can duplicate delivery to recipients who already accepted it;
    SMTP cannot guarantee exactly-once delivery after an ambiguous disconnect.
    """

    def __init__(self, settings: EmailSettings, password: str):
        self.settings = settings
        self._password = password

    def send_available(self, observations: list[Observation]) -> None:
        """Notify for known available targets; an empty batch sends nothing."""
        available = [item for item in observations if item.available is True]
        if not available:
            return
        lines = [
            "AutoDL 检测到以下监控目标有足够的空闲 GPU：",
            "",
        ]
        for item in available:
            idle = "未知" if item.idle_gpus is None else str(item.idle_gpus)
            required = "未知" if item.required_gpus is None else str(item.required_gpus)
            lines.extend(
                [
                    f"目标：{item.label}",
                    f"标识：{item.target_key}",
                    f"空闲 GPU：{idle}；需要 GPU：{required}",
                    f"状态：{item.detail}",
                    "",
                ]
            )
        lines.extend(
            [
                f"检测时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                "请到 AutoDL 控制台核实并手动开机：",
                "https://www.autodl.com/console/instance/list",
                "空闲状态可能随时变化。此邮件只代表空闲检测结果，不代表实例已开机。",
            ]
        )
        self._send(f"AutoDL Aoao GPU 可用提醒（{len(available)} 个目标）", "\n".join(lines))

    def send_auto_started(self, instance_uuid: str, label: str) -> None:
        """Send only after the monitor has confirmed GPU running and saved it."""
        self._send(
            "AutoDL Aoao 已自动开机",
            f"已自动开机，并已确认以下实例以 GPU 模式运行：\n\n实例：{label}\n实例 ID：{instance_uuid}\n\n"
            "本轮自动抢卡已结束，不会再启动其他实例；监控仍会继续。\n"
            "运行费用按 AutoDL 原有计费规则计算，不使用时请及时关机。\n"
            "https://www.autodl.com/console/instance/list\n",
        )

    def send_test(self) -> None:
        """Send a test email without accessing AutoDL."""
        self._send(
            "AutoDL Aoao 测试邮件",
            "这是一封 AutoDL Aoao 测试邮件。\n"
            "收到此邮件表示 SMTP 发送配置可用。\n"
            "本次测试不会查询 AutoDL，也不会开关机。\n",
        )

    def _validate_settings(self) -> None:
        settings = self.settings
        if settings.security not in ("ssl", "starttls"):
            raise MailError("SMTP 加密方式只能为 ssl 或 starttls。")
        if not isinstance(settings.host, str) or not settings.host.strip():
            raise MailError("请填写 SMTP 服务器地址。")
        if isinstance(settings.port, bool) or not isinstance(settings.port, int) or not 1 <= settings.port <= 65535:
            raise MailError("SMTP 端口必须是 1～65535 之间的整数。")
        if (
            isinstance(settings.timeout, bool)
            or not isinstance(settings.timeout, (int, float))
            or not math.isfinite(settings.timeout)
            or settings.timeout <= 0
        ):
            raise MailError("SMTP 超时时间必须是有限的正数。")
        if not isinstance(self._password, str) or not self._password:
            raise MailError("缺少 SMTP 授权码，请设置配置中指定的环境变量。")
        if not settings.recipients or isinstance(settings.recipients, str):
            raise MailError("请至少填写一个收件邮箱。")
        addresses = [settings.sender, *settings.recipients]
        if any(
            not isinstance(address, str)
            or not address.strip()
            or "@" not in address
            or any(char in address for char in ("\r", "\n", "\x00"))
            for address in addresses
        ):
            raise MailError("发件人和收件人必须是有效邮箱地址，不能含换行符。")

    def _send(self, subject: str, body: str) -> None:
        self._validate_settings()
        client: smtplib.SMTP | None = None
        try:
            message = EmailMessage(policy=SMTP)
            message["Subject"] = subject
            message["From"] = self.settings.sender
            message["To"] = ", ".join(self.settings.recipients)
            message["Date"] = format_datetime(datetime.now(timezone.utc))
            message["Message-ID"] = make_msgid()
            # Quoted-printable keeps UTF-8 body text compatible with servers
            # that do not advertise 8BITMIME; headers use MIME encoding.
            message.set_content(body, charset="utf-8", cte="quoted-printable")
            context = ssl.create_default_context()
            if self.settings.security == "ssl":
                client = smtplib.SMTP_SSL(
                    self.settings.host,
                    self.settings.port,
                    timeout=self.settings.timeout,
                    context=context,
                )
            else:
                client = smtplib.SMTP(
                    self.settings.host,
                    self.settings.port,
                    timeout=self.settings.timeout,
                )
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(self.settings.sender, self._password)
            refused = client.send_message(
                message,
                from_addr=self.settings.sender,
                to_addrs=list(self.settings.recipients),
            )
            if refused:
                raise MailError(
                    f"SMTP 拒绝了 {len(refused)} 个收件人，邮件未全部送达。"
                    "重试可能让已接收的收件人收到重复邮件。"
                )
            # Once DATA has been accepted, a QUIT failure does not undo delivery.
            # Do not make the monitor resend solely because teardown failed.
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                pass
        except MailError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise MailError(
                "SMTP 登录失败，请检查发件邮箱、SMTP 服务是否开启及客户端授权码。"
            ) from None
        except smtplib.SMTPRecipientsRefused:
            raise MailError("SMTP 拒绝了全部收件人，邮件未被接收。") from None
        except ssl.SSLCertVerificationError:
            raise MailError("SMTP TLS 证书验证失败，请检查服务器地址及系统证书。") from None
        except ssl.SSLError:
            raise MailError("SMTP TLS 连接失败，请检查服务器、端口及加密方式。") from None
        except smtplib.SMTPNotSupportedError:
            raise MailError("SMTP 服务器不支持所需的 TLS、登录验证或邮件编码。") from None
        except (TimeoutError, smtplib.SMTPServerDisconnected):
            raise MailError(
                "SMTP 超时或连接中断，邮件是否送达无法确认；重试可能发送重复邮件。"
            ) from None
        except smtplib.SMTPException:
            # Never include server responses: they may echo AUTH data or mail.
            raise MailError("SMTP 拒绝了邮件，或邮件发送过程失败。") from None
        except OSError:
            raise MailError("无法连接 SMTP 服务器，请检查服务器地址、端口及网络。") from None
        except (ValueError, TypeError, UnicodeError):
            raise MailError("邮件生成或发送失败，请检查 SMTP 配置及邮箱地址。") from None
        finally:
            if client is not None:
                try:
                    client.close()
                except (smtplib.SMTPException, OSError):
                    pass
