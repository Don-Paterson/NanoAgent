"""
NanoAgent negotiate function (Azure Functions, Python v2 model).

GET /api/negotiate?code=<function key>&role=agent|controller&group=lab-a-gui
 -> {"url": "wss://<pubsub>.webpubsub.azure.com/client/hubs/nano?access_token=..."}

The token only allows joining and sending to that one group, and expires
(NANO_TOKEN_MINUTES, default 60).  Commands are additionally HMAC-signed end to
end, so this token alone cannot drive an agent.

App settings: WEBPUBSUB_CONNECTION, NANO_HUB (nano), NANO_GROUP_PREFIXES (lab-,sup-),
              NANO_TOKEN_MINUTES (60)
"""
import json
import logging
import os
import re
import uuid

import azure.functions as func
from azure.messaging.webpubsubservice import WebPubSubServiceClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
GROUP_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_client = None


def _service():
    global _client
    if _client is None:
        _client = WebPubSubServiceClient.from_connection_string(
            os.environ["WEBPUBSUB_CONNECTION"], hub=os.environ.get("NANO_HUB", "nano"))
    return _client


def _json(status: int, body: dict) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json",
                             headers={"Cache-Control": "no-store"})


@app.route(route="negotiate", methods=["GET", "POST"])
def negotiate(req: func.HttpRequest) -> func.HttpResponse:
    role = req.params.get("role", "controller")
    group = req.params.get("group", "")
    if role not in ("agent", "controller"):
        return _json(400, {"error": "role must be agent or controller"})
    if not GROUP_RE.match(group):
        return _json(400, {"error": "group must be 3-64 chars of A-Z a-z 0-9 _ -"})
    prefixes = [p.strip() for p in os.environ.get("NANO_GROUP_PREFIXES", "lab-,sup-").split(",") if p.strip()]
    if prefixes and not any(group.startswith(p) for p in prefixes):
        return _json(403, {"error": "group not allowed"})
    minutes = int(os.environ.get("NANO_TOKEN_MINUTES", "60"))
    token = _service().get_client_access_token(
        user_id=f"{role}-{uuid.uuid4().hex[:8]}",
        roles=[f"webpubsub.joinLeaveGroup.{group}", f"webpubsub.sendToGroup.{group}"],
        minutes_to_expire=minutes)
    logging.info("negotiate role=%s group=%s", role, group)
    return _json(200, {"url": token["url"]})
