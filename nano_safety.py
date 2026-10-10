"""nano_safety.py - log redaction and the TLS-inspection guard for NanoAgent.

Redaction: the agent writes every command it carries out to nano_agent.log.  Typed text
and anything that looks like a password or token is removed before it is logged.

TLS guard: before the agent sends its function key to the negotiate endpoint (or opens the
WebSocket) it checks who issued the server's certificate.  Traffic is end-to-end encrypted,
so an inspecting firewall cannot read screenshots or commands, but it could read the
function key in the negotiate URL.  If the certificate was issued by anyone other than a
trusted issuer (for Azure: Microsoft or DigiCert) the guard stops the connection, or only
warns, depending on `tls_guard` in agent.json:

    "tls_guard": "enforce"          # default: refuse; also "warn" (log only) or "off"
    "tls_trusted_issuers": ["Microsoft Corporation", "DigiCert Inc"]
"""
from __future__ import annotations

import logging
import re
import socket
import ssl
import time
import urllib.parse
from typing import Callable, Optional

log = logging.getLogger("nano.safety")

DEFAULT_TRUSTED = ("Microsoft Corporation", "DigiCert Inc")
CACHE_SECONDS = 600


# --------------------------------------------------------------------------- redaction
_PATTERNS = [
    # password = 'x', $pw = "x", token: x, -Password 'x', --password=x
    re.compile(r"(?i)(\b\w*(?:password|passwd|pwd|secret|token|apikey|api_key|accesskey)\w*\s*[:=]\s*)('[^']*'|\"[^\"]*\"|\S+)"),
    re.compile(r"(?i)(-{1,2}\w*(?:password|passwd|pwd|secret|token|pw)\w*[\s=]+)('[^']*'|\"[^\"]*\"|\S+)"),
    re.compile(r"(?i)(ConvertTo-SecureString\s+)('[^']*'|\"[^\"]*\"|\S+)"),
    re.compile(r"(?i)(\bsshpass\s+-p\s*)(\S+)"),
]
_SENSITIVE_KEYS = re.compile(r"(?i)password|passwd|secret|token|key|data")


def redact_text(s: str, limit: int = 300) -> str:
    for rx in _PATTERNS:
        s = rx.sub(lambda m: m.group(1) + "<redacted>", s)
    return s if len(s) <= limit else s[:limit] + f"...<{len(s)} chars>"


def redact_args(op: str, args: dict) -> dict:
    """Arguments of a command, safe to write to the log."""
    out = {}
    for k, v in (args or {}).items():
        if op == "type" and k == "text":
            out[k] = f"<{len(str(v))} chars>"            # typed text may be a password: never logged
        elif _SENSITIVE_KEYS.search(k) and k not in ("data_type",):
            out[k] = f"<{len(str(v))} chars>"
        elif isinstance(v, str):
            out[k] = redact_text(v)
        elif isinstance(v, (list, tuple)):
            out[k] = redact_text(repr(v))
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- TLS guard
class TlsGuardError(RuntimeError):
    pass


def peer_issuer(host: str, port: int = 443, timeout: float = 8) -> dict:
    """Who issued the certificate `host` presents?  {ok, org, issuer, error}.

    ok is True/False for a definite answer and None when the server could not be reached at all
    (the negotiate call will then fail by itself with a clearer message)."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    def issuer_of(ctx):
        with socket.create_connection((host, port), timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as t:
                der = t.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)
        orgs = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        return (orgs[0].value if orgs else ""), cert.issuer.rfc4514_string()

    try:
        org, full = issuer_of(ssl.create_default_context())
        return {"ok": True, "org": org, "issuer": full, "error": None}
    except ssl.SSLCertVerificationError as e:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            org, full = issuer_of(ctx)
        except Exception:
            org, full = "", ""
        return {"ok": False, "org": org, "issuer": full, "error": f"certificate not valid: {e.verify_message}"}
    except (OSError, ssl.SSLError) as e:
        return {"ok": None, "org": "", "issuer": "", "error": f"{type(e).__name__}: {e}"}


def make_guard(cfg: dict, status_cb: Optional[Callable[[str], None]] = None,
               checker: Callable[..., dict] = peer_issuer):
    """Return guard(url), or None when the guard is off.  guard raises TlsGuardError to block."""
    mode = str(cfg.get("tls_guard", "enforce")).lower()
    if mode == "off":
        return None
    trusted = {o.lower() for o in (cfg.get("tls_trusted_issuers") or DEFAULT_TRUSTED)}
    cache: dict[str, tuple[float, dict]] = {}
    state = {"results": {}}

    def guard(url: str):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("https", "wss") or not u.hostname:
            return                                           # plain ws:// = local relay, nothing to inspect
        host, port = u.hostname, u.port or 443
        hit = cache.get(host)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return
        r = checker(host, port)
        state["results"][host] = r
        if r["ok"] is None:
            log.warning("TLS guard: could not check %s (%s); carrying on", host, r["error"])
            return
        if r["ok"] and (r["org"] or "").lower() in trusted:
            cache[host] = (time.time(), r)
            log.info("TLS guard: %s ok (issuer %s)", host, r["org"])
            return
        why = r["error"] or f"unexpected issuer '{r['org'] or r['issuer']}'"
        msg = f"TLS guard: {host}: {why}. Something between this machine and Azure may be inspecting HTTPS."
        if mode == "warn":
            log.warning("%s Continuing (tls_guard=warn).", msg)
            cache[host] = (time.time(), r)
            return
        if status_cb:
            status_cb(f"NanoAgent: connection blocked, {host} certificate from {r['org'] or 'unknown issuer'}")
        raise TlsGuardError(msg + " Not connecting (tls_guard=enforce).")

    guard.state = state
    return guard
