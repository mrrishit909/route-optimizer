"""Customer text messages through Twilio's REST API. Without credentials the message is only recorded."""
from __future__ import annotations

import os

import httpx


class Notifier:
    def __init__(self, sid: str | None = None, token: str | None = None, sender: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.sid = sid or os.environ.get("TWILIO_ACCOUNT_SID")
        self.token = token or os.environ.get("TWILIO_AUTH_TOKEN")
        self.sender = sender or os.environ.get("TWILIO_FROM")
        self.transport = transport

    def send(self, to: str, body: str) -> str:
        """Returns the delivery status that goes into the outbox."""
        if not (self.sid and self.token and self.sender):
            return "not sent (no Twilio credentials)"
        try:
            with httpx.Client(timeout=10, transport=self.transport, auth=(self.sid, self.token)) as client:
                r = client.post(f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                                data={"To": to, "From": self.sender, "Body": body})
            return "sent" if r.status_code == 201 else f"failed (HTTP {r.status_code})"
        except httpx.HTTPError as exc:
            return f"failed ({type(exc).__name__})"
