"""推送渠道实现：file / webhook / serverchan / bark / ntfy / feishu / wecom / dingtalk / telegram / email。"""
from __future__ import annotations

import json
import re
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..settings import ROOT, Settings
from ..textutil import now_utc, slugify, truncate


class PushError(Exception):
    pass


class Channel:
    type = "base"

    def __init__(self, cfg: dict, settings: Settings):
        self.cfg = cfg
        self.key = cfg.get("key", self.type)
        self.settings = settings

    def configured(self) -> bool:
        return True

    def send(self, title: str, markdown: str, payload: dict) -> str:
        raise NotImplementedError

    @staticmethod
    def _post(url: str, *, json_body=None, data=None, headers=None, timeout=20) -> httpx.Response:
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(url, json=json_body, data=data, headers=headers)
        except httpx.HTTPError as e:
            raise PushError(str(e)) from e
        if r.status_code >= 400:
            raise PushError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r


def _plain(md: str) -> str:
    """Markdown → 近似纯文本（给不支持 Markdown 的渠道）。"""
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", md)
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    t = re.sub(r"[*_`>]+", "", t)
    return t.strip()


class FileChannel(Channel):
    type = "file"

    def send(self, title, markdown, payload):
        d = Path(self.cfg.get("dir") or (self.settings.data_dir / "outbox"))
        if not d.is_absolute():
            d = ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        name = f"{now_utc().strftime('%Y%m%d-%H%M%S')}-{slugify(title)[:60]}.md"
        p = d / name
        p.write_text(f"# {title}\n\n{markdown}\n", encoding="utf-8")
        return str(p)


class WebhookChannel(Channel):
    type = "webhook"

    def configured(self):
        return bool(self.cfg.get("url"))

    def send(self, title, markdown, payload):
        body = {"event": payload.get("event"), "title": title, "markdown": markdown, **{k: v for k, v in payload.items() if k != "event"}}
        self._post(self.cfg["url"], json_body=body, headers={"Content-Type": "application/json"})
        return "ok"


class ServerChanChannel(Channel):
    type = "serverchan"

    def configured(self):
        return bool(self.cfg.get("sendkey"))

    def send(self, title, markdown, payload):
        key = self.cfg["sendkey"]
        base = f"https://{key.split('T')[0]}.push.ft07.com/send/{key}.send" if key.startswith("sctp") else f"https://sctapi.ftqq.com/{key}.send"
        self._post(base, data={"title": truncate(title, 32), "desp": markdown})
        return "ok"


class BarkChannel(Channel):
    type = "bark"

    def configured(self):
        return bool(self.cfg.get("url"))

    def send(self, title, markdown, payload):
        self._post(self.cfg["url"].rstrip("/"), json_body={"title": title, "body": truncate(_plain(markdown), 3000), "group": "ReproHub"})
        return "ok"


class NtfyChannel(Channel):
    type = "ntfy"

    def configured(self):
        return bool(self.cfg.get("url"))

    def send(self, title, markdown, payload):
        parts = urlsplit(self.cfg["url"])
        topic = parts.path.strip("/")
        base = f"{parts.scheme}://{parts.netloc}"
        self._post(base, json_body={"topic": topic, "title": title, "message": truncate(markdown, 4000), "markdown": True})
        return "ok"


class FeishuChannel(Channel):
    type = "feishu"

    def configured(self):
        return bool(self.cfg.get("webhook"))

    def send(self, title, markdown, payload):
        self._post(self.cfg["webhook"], json_body={"msg_type": "text", "content": {"text": f"{title}\n\n{truncate(_plain(markdown), 3500)}"}})
        return "ok"


class WeComChannel(Channel):
    type = "wecom"

    def configured(self):
        return bool(self.cfg.get("webhook"))

    def send(self, title, markdown, payload):
        self._post(self.cfg["webhook"], json_body={"msgtype": "markdown", "markdown": {"content": truncate(f"**{title}**\n{markdown}", 4000)}})
        return "ok"


class DingTalkChannel(Channel):
    type = "dingtalk"

    def configured(self):
        return bool(self.cfg.get("webhook"))

    def send(self, title, markdown, payload):
        self._post(self.cfg["webhook"], json_body={"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{truncate(markdown, 18000)}"}})
        return "ok"


class TelegramChannel(Channel):
    type = "telegram"

    def configured(self):
        return bool(self.cfg.get("bot_token") and self.cfg.get("chat_id"))

    def send(self, title, markdown, payload):
        text = truncate(f"{title}\n\n{_plain(markdown)}", 4000)
        self._post(f"https://api.telegram.org/bot{self.cfg['bot_token']}/sendMessage", json_body={"chat_id": self.cfg["chat_id"], "text": text, "disable_web_page_preview": True})
        return "ok"


class EmailChannel(Channel):
    type = "email"

    def configured(self):
        return all(self.cfg.get(k) for k in ("host", "user", "password", "to"))

    def send(self, title, markdown, payload):
        msg = MIMEText(markdown, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        sender = self.cfg.get("from") or self.cfg["user"]
        msg["From"] = formataddr((str(Header("ReproHub", "utf-8")), sender))
        tos = [t.strip() for t in str(self.cfg["to"]).split(",") if t.strip()]
        msg["To"] = ", ".join(tos)
        port = int(self.cfg.get("port") or 465)
        try:
            if port == 465:
                with smtplib.SMTP_SSL(self.cfg["host"], port, context=ssl.create_default_context(), timeout=30) as s:
                    s.login(self.cfg["user"], self.cfg["password"])
                    s.sendmail(sender, tos, msg.as_string())
            else:
                with smtplib.SMTP(self.cfg["host"], port, timeout=30) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(self.cfg["user"], self.cfg["password"])
                    s.sendmail(sender, tos, msg.as_string())
        except (smtplib.SMTPException, OSError) as e:
            raise PushError(str(e)) from e
        return "ok"


CHANNEL_TYPES: dict[str, type[Channel]] = {
    c.type: c for c in (FileChannel, WebhookChannel, ServerChanChannel, BarkChannel, NtfyChannel, FeishuChannel, WeComChannel, DingTalkChannel, TelegramChannel, EmailChannel)
}


def build_channels(settings: Settings) -> dict[str, Channel]:
    out: dict[str, Channel] = {}
    for cfg in (settings.push.get("channels") or []):
        cls = CHANNEL_TYPES.get(cfg.get("type", ""))
        if not cls:
            continue
        ch = cls(cfg, settings)
        out[ch.key] = ch
    return out


def channel_status(settings: Settings) -> list[dict]:
    rows = []
    for key, ch in build_channels(settings).items():
        rows.append({"key": key, "type": ch.type, "enabled": bool(ch.cfg.get("enabled", False)), "configured": ch.configured()})
    return rows


__all__ = ["Channel", "PushError", "CHANNEL_TYPES", "build_channels", "channel_status", "json"]
