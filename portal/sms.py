"""sms.py — text messages (SMS) through Twilio's REST API. Off unless configured, and per elevator.

Settings (env or Streamlit secrets; none are set until JSA has registered its texting campaign):
  SMS_ENABLED=1                  master switch (anything else = no texts, queued or sent)
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
  TWILIO_FROM                    JSA's shared number (E.164, +1...) OR
  TWILIO_MESSAGING_SERVICE_SID   a Messaging Service (use this once numbers are pooled)
An elevator with its own number (tenants.sms_from) sends from that number instead of the shared one.

Carrier rules this module leaves to its callers: only people who opted in are texted (notify.py checks), every message
says how to stop, and Twilio itself answers STOP/HELP replies and then refuses further sends with error 21610, which
`StopRequested` reports so the caller can clear that person's opt-in.
"""
from __future__ import annotations

import os
import re

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
STOP_ERRORS = {21610}                    # "recipient has unsubscribed"


class SmsError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class StopRequested(SmsError):
    """The recipient replied STOP at some point: do not text them again until they opt in afresh."""


def enabled() -> bool:
    return (os.environ.get("SMS_ENABLED") or "").strip() == "1"


def configured() -> bool:
    return bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN")
                and (os.environ.get("TWILIO_FROM") or os.environ.get("TWILIO_MESSAGING_SERVICE_SID")))


def ready() -> bool:
    return enabled() and configured()


def normalize_phone(raw: str | None) -> str | None:
    """A US/Canada number in E.164 (+15551234567), or any international +number of 8-15 digits; None if unusable."""
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10 and digits[0] in "23456789":
        return f"+1{digits}"
    if len(digits) == 11 and digits[0] == "1" and digits[1] in "23456789":
        return f"+{digits}"
    return None


def pretty(e164: str | None) -> str:
    d = re.sub(r"\D", "", e164 or "")
    return f"({d[1:4]}) {d[4:7]}-{d[7:]}" if len(d) == 11 and d[0] == "1" else (e164 or "")


def send(to: str, body: str, from_number: str | None = None, session=None) -> str:
    """Send one text; returns Twilio's message id. `from_number` (an elevator's own) beats the shared setting."""
    if not ready():
        raise SmsError("Texting is not switched on for this deployment.")
    import requests
    http = session or requests
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    payload = {"To": to, "Body": body}
    if from_number:
        payload["From"] = from_number
    elif os.environ.get("TWILIO_MESSAGING_SERVICE_SID"):
        payload["MessagingServiceSid"] = os.environ["TWILIO_MESSAGING_SERVICE_SID"]
    else:
        payload["From"] = os.environ["TWILIO_FROM"]
    r = http.post(API.format(sid=sid), data=payload, auth=(sid, token), timeout=20)
    if r.status_code >= 400:
        try:
            err = r.json()
        except ValueError:
            err = {}
        code, msg = err.get("code"), (err.get("message") or f"HTTP {r.status_code}")
        raise (StopRequested if code in STOP_ERRORS else SmsError)(f"Twilio {code}: {msg}"[:300], code)
    return (r.json() or {}).get("sid", "")
