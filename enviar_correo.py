import os
import json
import smtplib
from io import BytesIO
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication


# =========================
# CONFIGURACIÓN DE CORREO
# =========================

def enviar_correo_html(destinatarios, asunto, html, adjuntos=None, cc=None, bcc=None, reply_to=None):
    """
    Envía correo HTML usando variables del .env
    Acepta MAIL_* o SMTP_* (para que no vuelva a romperse).
    """

    # 🔑 Credenciales (prioridad: MAIL_*)
    smtp_user = os.getenv("MAIL_USERNAME") or os.getenv("SMTP_USER")
    smtp_pass = os.getenv("MAIL_PASSWORD") or os.getenv("SMTP_PASS")

    smtp_host = os.getenv("MAIL_SERVER") or os.getenv("SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(os.getenv("MAIL_PORT") or os.getenv("SMTP_PORT") or 587)

    if not smtp_user or not smtp_pass:
        return False, "SMTP no configurado (faltan MAIL_USERNAME / MAIL_PASSWORD)."

    # Normalizar destinatarios
    to_list = _norm_emails(destinatarios)
    cc_list = _norm_emails(cc)
    bcc_list = _norm_emails(bcc)
    all_recipients = to_list + cc_list + bcc_list

    if not all_recipients:
        return False, "No hay destinatarios."

    # Construir mensaje
    msg = MIMEMultipart("mixed")
    msg["Subject"] = asunto
    msg["From"] = smtp_user
    msg["To"] = ", ".join(to_list)

    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    if reply_to:
        msg["Reply-To"] = reply_to

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html or "", "html", "utf-8"))
    msg.attach(alt)

    # Adjuntos
    for a in (adjuntos or []):
        try:
            part = MIMEApplication(a["content"])
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=a.get("filename", "archivo"),
            )
            msg.attach(part)
        except Exception as e:
            print("⚠️ Error adjunto:", e)

    # Envío
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, all_recipients, msg.as_string())

        print("✅ Correo enviado correctamente")
        return True, ""

    except smtplib.SMTPAuthenticationError as e:
        return False, f"Error autenticación SMTP (App Password): {e}"

    except Exception as e:
        return False, f"Error enviando correo: {e}"


# =========================
# UTILIDADES
# =========================

def _norm_emails(value):
    if not value:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]
