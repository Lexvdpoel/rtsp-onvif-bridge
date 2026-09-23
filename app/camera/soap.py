"""SOAP envelope helpers and ONVIF WS-Security handling."""

from __future__ import annotations

import base64
import hashlib
import time
import xml.etree.ElementTree as ET

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "tan": "http://www.onvif.org/ver20/analytics/wsdl",
}

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)

_NS_ATTRS = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NS.items())


def envelope(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<s:Envelope {_NS_ATTRS}>"
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")


def fault(code: str, subcode: str, reason: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<s:Envelope {_NS_ATTRS}>"
        "<s:Body><s:Fault>"
        f"<s:Code><s:Value>s:{code}</s:Value>"
        f"<s:Subcode><s:Value>{subcode}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason>'
        "</s:Fault></s:Body></s:Envelope>"
    ).encode("utf-8")


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse(body: bytes):
    """Return (action_name, action_element, root) for a SOAP request."""
    root = ET.fromstring(body)
    soap_body = root.find("s:Body", NS)
    if soap_body is None or len(soap_body) == 0:
        return "", None, root
    action = soap_body[0]
    return localname(action.tag), action, root


def child_text(element, name: str, default: str = "") -> str:
    """First descendant with the given local name, regardless of namespace."""
    if element is None:
        return default
    for node in element.iter():
        if localname(node.tag) == name:
            return (node.text or "").strip()
    return default


def child_attr(element, name: str, attr: str, default: str = "") -> str:
    if element is None:
        return default
    for node in element.iter():
        if localname(node.tag) == name:
            return node.get(attr, default)
    return default


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_ws_security(root, username: str, password: str) -> tuple[bool, bool]:
    """Validate a WS-UsernameToken header.

    Returns (token_present, token_valid). Supports both PasswordDigest (what
    ONVIF clients normally send) and plaintext PasswordText.
    """
    token = None
    for node in root.iter():
        if localname(node.tag) == "UsernameToken":
            token = node
            break
    if token is None:
        return False, False

    sent_user = ""
    sent_pass = ""
    pass_type = ""
    nonce_b64 = ""
    created = ""
    for node in token.iter():
        name = localname(node.tag)
        text = (node.text or "").strip()
        if name == "Username":
            sent_user = text
        elif name == "Password":
            sent_pass = text
            pass_type = node.get("Type", "")
        elif name == "Nonce":
            nonce_b64 = text
        elif name == "Created":
            created = text

    if sent_user != username:
        return True, False

    if pass_type.endswith("#PasswordDigest") or (nonce_b64 and created):
        try:
            nonce = base64.b64decode(nonce_b64)
        except Exception:
            return True, False
        digest = hashlib.sha1(nonce + created.encode() + password.encode()).digest()
        return True, base64.b64encode(digest).decode() == sent_pass

    return True, sent_pass == password


def check_http_basic(header: str, username: str, password: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
    except Exception:
        return False
    user, _, pwd = decoded.partition(":")
    return user == username and pwd == password


def check_http_digest(header: str, method: str, username: str, password: str,
                      nonces: set[str]) -> bool:
    """Validate an RFC 2617 Digest Authorization header (qop=auth or legacy)."""
    if not header.lower().startswith("digest "):
        return False
    params: dict[str, str] = {}
    for part in _split_digest(header.split(" ", 1)[1]):
        key, _, value = part.partition("=")
        params[key.strip().lower()] = value.strip().strip('"')

    if params.get("username") != username or params.get("nonce") not in nonces:
        return False

    ha1 = hashlib.md5(f"{username}:{params.get('realm','')}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{params.get('uri','')}".encode()).hexdigest()
    if params.get("qop") in ("auth", "auth-int"):
        raw = (
            f"{ha1}:{params.get('nonce','')}:{params.get('nc','')}:"
            f"{params.get('cnonce','')}:{params.get('qop')}:{ha2}"
        )
    else:
        raw = f"{ha1}:{params.get('nonce','')}:{ha2}"
    return hashlib.md5(raw.encode()).hexdigest() == params.get("response")


def _split_digest(value: str) -> list[str]:
    """Split a digest header on commas that are not inside quotes."""
    parts, buf, in_quotes = [], [], False
    for char in value:
        if char == '"':
            in_quotes = not in_quotes
        if char == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if buf:
        parts.append("".join(buf))
    return parts


def xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
