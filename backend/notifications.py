import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


RESEND_API_URL = "https://api.resend.com/emails"


def send_security_alert(incident):
    api_key = os.getenv("RESEND_API_KEY")
    recipients = [
        address.strip()
        for address in os.getenv("SECURITY_ALERT_EMAILS", "").split(",")
        if address.strip()
    ]
    sender = os.getenv("RESEND_FROM_EMAIL")
    if not api_key or not recipients or not sender:
        raise RuntimeError(
            "Security email is not configured. Set RESEND_API_KEY, "
            "RESEND_FROM_EMAIL, and SECURITY_ALERT_EMAILS."
        )

    event_type = incident["event_type"].replace("_", " ").title()
    subject = f"{incident['severity']} security incident: {event_type}"
    text = "\n".join(
        (
            f"Incident: {event_type}",
            f"Severity: {incident['severity']}",
            f"Description: {incident['description']}",
            f"Recommended action: {incident['recommended_action']}",
            f"Incident ID: {incident['id']}",
        )
    )
    payload = json.dumps(
        {
            "from": sender,
            "to": recipients,
            "subject": subject,
            "text": text,
        }
    ).encode("utf-8")
    request = Request(
        RESEND_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"Resend could not deliver the security email (HTTP {response.status})."
                )
    except HTTPError as error:
        raise RuntimeError(
            f"Resend could not deliver the security email (HTTP {error.code})."
        ) from error
    except URLError as error:
        raise RuntimeError("Could not reach Resend to send the security email.") from error
    except OSError as error:
        raise RuntimeError("Could not reach Resend to send the security email.") from error