"""Alerts: phone push (ntfy / Telegram), Slack / Discord webhooks, email, desktop pop-ups, console.

Every configured channel gets every alert; one failing channel never blocks the others.
Secrets come from environment variables, never from config.yaml:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JOBAGENT_WEBHOOK_URL, SMTP_PASSWORD, NTFY_TOKEN (optional)
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import smtplib
import subprocess
from email.message import EmailMessage

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)


# Windows 10/11 toast via built-in PowerShell + WinRT (no extra installs). The XML travels in an
# environment variable so titles with quotes / emoji can't break the command line.
_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:JOBAGENT_TOAST_XML)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def toast_xml(title: str, message: str, url: str = "", urgent: bool = False) -> str:
    from xml.sax.saxutils import escape, quoteattr
    launch = f' activationType="protocol" launch={quoteattr(url)}' if url else ""
    scenario = ' scenario="reminder"' if urgent else ""  # stays on screen until dismissed
    actions = (f'<actions><action content="Open job" activationType="protocol" arguments={quoteattr(url)}/>'
               '<action content="Dismiss" activationType="system" arguments="dismiss"/></actions>') if url and urgent else ""
    return (f'<toast{launch}{scenario}><visual><binding template="ToastGeneric">'
            f"<text>{escape(title)}</text><text>{escape(message[:400])}</text></binding></visual>"
            f'<audio src="ms-winsoundevent:Notification.{"Reminder" if urgent else "Default"}"/>{actions}</toast>')


def _windows_toast(title: str, message: str, url: str, urgent: bool) -> None:
    env = dict(os.environ, JOBAGENT_TOAST_XML=toast_xml(title, message, url, urgent))
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_TOAST],
                   env=env, check=True, timeout=20, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class EmailConfig(BaseModel):
    smtp_host: str = ""        # e.g. smtp.gmail.com (use an app password)
    smtp_port: int = 587
    username: str = ""
    to: str = ""


class NotifyConfig(BaseModel):
    console: bool = True
    desktop: bool = True
    ntfy_topic: str = ""       # pick a long random name; install the ntfy app and subscribe to it
    ntfy_server: str = "https://ntfy.sh"
    telegram: bool = False     # needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    webhook: bool = False      # Slack / Discord incoming webhook in JOBAGENT_WEBHOOK_URL
    email: EmailConfig = EmailConfig()


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg
        self.http = httpx.Client(timeout=15)

    def channels(self) -> list[str]:
        c = self.cfg
        out = [n for n, on in [("console", c.console), ("desktop", c.desktop), ("ntfy", bool(c.ntfy_topic)),
                               ("telegram", c.telegram), ("webhook", c.webhook),
                               ("email", bool(c.email.smtp_host and c.email.to))] if on]
        return out

    def send(self, title: str, message: str, url: str = "", urgent: bool = False) -> dict[str, bool]:
        results = {}
        for ch in self.channels():
            try:
                getattr(self, f"_{ch}")(title, message, url, urgent)
                results[ch] = True
            except Exception as e:  # noqa: BLE001 - alerts are best-effort, per channel
                log.warning("alert via %s failed: %s", ch, e)
                results[ch] = False
        return results

    # ------------------------------------------------------------------ channels --
    def _console(self, title, message, url, urgent):
        bar = "!" * 60 if urgent else "-" * 60
        print(f"\n{bar}\n{title}\n{message}" + (f"\n{url}" if url else "") + f"\n{bar}", flush=True)

    def _desktop(self, title, message, url, urgent):
        system = platform.system()
        if system == "Windows":
            _windows_toast(title, message, url, urgent)
        elif system == "Darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            script = f'display notification "{esc(message)}" with title "{esc(title)}"'
            if urgent:
                script += ' sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=True, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "jobagent", "-u", "critical" if urgent else "normal",
                            title, message + (f"\n{url}" if url else "")], check=True, timeout=10)
        else:
            raise RuntimeError("no desktop notifier found (install libnotify / notify-send)")

    def _ntfy(self, title, message, url, urgent):
        # JSON publishing (https://docs.ntfy.sh/publish/#publish-as-json) keeps emoji/UTF-8 intact.
        body = {"topic": self.cfg.ntfy_topic, "title": title, "message": message,
                "priority": 5 if urgent else 3, "tags": ["briefcase"]}
        if url:
            body["click"] = url
        headers = {"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}"} if os.environ.get("NTFY_TOKEN") else {}
        r = self.http.post(self.cfg.ntfy_server.rstrip("/") + "/", json=body, headers=headers)
        r.raise_for_status()

    def _telegram(self, title, message, url, urgent):
        token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        text = f"{title}\n\n{message}" + (f"\n{url}" if url else "")
        r = self.http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        r.raise_for_status()

    def _webhook(self, title, message, url, urgent):
        hook = os.environ["JOBAGENT_WEBHOOK_URL"]
        text = f"*{title}*\n{message}" + (f"\n{url}" if url else "")
        payload = {"content": text.replace("*", "**")} if "discord" in hook else {"text": text}
        r = self.http.post(hook, json=payload)
        r.raise_for_status()

    def _email(self, title, message, url, urgent):
        e = self.cfg.email
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = e.username or e.to
        msg["To"] = e.to
        msg.set_content(message + (f"\n\n{url}" if url else ""))
        with smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=20) as s:
            s.starttls()
            if e.username:
                s.login(e.username, os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
