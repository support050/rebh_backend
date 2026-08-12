"""SMTP email delivery — never logs bodies, tokens, or credentials."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_email(
    to_email: str,
    subject: str,
    body: str,
    *,
    user_id: int | None = None,
    purpose: str = "notification",
) -> bool:
    """
    Send an HTML email via SMTP.

    Blocking — call from BackgroundTasks / thread pool.
    Never logs the body (may contain reset/verification secrets).
    Returns True on success, False otherwise.
    """
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        uid = f"user_id={user_id}" if user_id is not None else f"to_domain={to_email.split('@')[-1]}"
        logger.warning(
            "Email delivery is not configured; %s email was not sent for %s",
            purpose,
            uid,
        )
        return False

    server: smtplib.SMTP | None = None
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))

        context = ssl.create_default_context()
        server = smtplib.SMTP(
            settings.SMTP_SERVER,
            settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        )
        if settings.SMTP_USE_TLS:
            server.starttls(context=context)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.FROM_EMAIL, to_email, msg.as_string())

        uid = f"user_id={user_id}" if user_id is not None else "user_id=unknown"
        logger.info("Email sent successfully (%s) for %s", purpose, uid)
        return True
    except Exception as e:
        logger.error("Failed to send email (%s): %s", purpose, type(e).__name__)
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
