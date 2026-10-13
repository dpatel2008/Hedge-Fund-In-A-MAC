"""
notify.py
Sends the summary email. The Gmail address and app password are saved by
setup_email.py in tracker_data/email_settings.json. That file stays on this
computer and is never uploaded to GitHub.
"""

import json
import os
import smtplib
import ssl
from email.message import EmailMessage

import certifi

import config as C

SETTINGS_FILE = os.path.join(C.DATA_DIR, "email_settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    if not all(data.get(key) for key in ("username", "password", "to")):
        return None
    return data


def build_message(settings, subject, body, attachments=None, html=None, images=None):
    """The email itself. body is the plain text. html (optional) is the same email as a page with the portfolio
    overview (most mail apps show that; the plain text is kept for the rest). images: {content id: PNG bytes}
    for pictures inside that page."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["username"]
    message["To"] = settings["to"]
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
        page = message.get_payload()[-1]
        for cid, data in (images or {}).items():
            page.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>", disposition="inline",
                             filename=cid.split("@")[0] + ".png")
    for path in attachments or []:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        message.add_attachment(data, maintype="text", subtype="html", filename=os.path.basename(path))
    return message


def send_email(subject, body, attachments=None, html=None, images=None):
    """Returns True if the email was sent."""
    settings = load_settings()
    if not settings:
        print("Email is not set up yet. Run: python tracker/setup_email.py")
        return False

    message = build_message(settings, subject, body, attachments, html, images)
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
            server.login(settings["username"], settings["password"])
            server.send_message(message)
        return True
    except (smtplib.SMTPException, OSError) as e:
        print(f"Could not send the email: {e}")
        return False
