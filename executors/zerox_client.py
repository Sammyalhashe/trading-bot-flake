"""0x Swap API v2 (AllowanceHolder) client with RFC 9421 Ed25519 response-signature verification.

Off-chain quote source only: nothing here reads chain state or sends transactions. Every 200
response must carry a valid 0x signature or it is rejected (fail closed).

Env:
  ZEROX_SIGNING_KEY_ID  trusted 0x response-signing key id(s), comma-separated
                        (default 0x-signing-key-prod-24092026)
  ZEROX_SIGNING_KEY_X   matching Ed25519 JWK `x` value(s) (base64url), same order; add the next key
                        here when docs.0x.org/docs/developer-resources/response-signatures lists it
"""
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ZEROX_API_URL = "https://api.0x.org"
PRICE_PATH = "/swap/allowance-holder/price"
QUOTE_PATH = "/swap/allowance-holder/quote"
SIGNING_KEY_ID = os.environ.get("ZEROX_SIGNING_KEY_ID", "0x-signing-key-prod-24092026")
SIGNING_KEY_X = os.environ.get("ZEROX_SIGNING_KEY_X", "19P3C981JsyQsqwVho8Tlabx51rslvdruSH59mGuWn0")
REQUIRED_SIGNATURE_COMPONENTS = (
    "@status", "content-type", "content-digest",
    "@method;req", "@authority;req", "@path;req", "@query;req",
)
SIGNATURE_MAX_SKEW_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = (3.05, 10)
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 5.0
MIN_REQUEST_INTERVAL_SECONDS = 0.2  # free tier allows ~5 requests/second


class ZeroExError(RuntimeError):
    pass


class SignatureVerificationError(ZeroExError):
    pass


def _b64url_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def trusted_signing_keys():
    key_ids, xs = SIGNING_KEY_ID.split(","), SIGNING_KEY_X.split(",")
    if len(key_ids) != len(xs):
        raise ValueError("ZEROX_SIGNING_KEY_ID and ZEROX_SIGNING_KEY_X list different numbers of keys")
    return {kid.strip(): Ed25519PublicKey.from_public_bytes(_b64url_decode(x.strip())) for kid, x in zip(key_ids, xs)}


def _parse_signature_input(header):
    if not header.startswith("sig="):
        raise SignatureVerificationError('Signature-Input has no "sig" signature')
    serialized = header[len("sig="):]
    match = re.fullmatch(r"\(([^)]*)\)(.*)", serialized)
    if not match:
        raise SignatureVerificationError("Signature-Input is malformed")
    if not re.fullmatch(r'(?:"[^"]+"(?:;req)?(?: |$))*', match.group(1)) or \
            not re.fullmatch(r'(?:;[a-z]+=(?:"[^"]*"|\d+))*', match.group(2)):
        raise SignatureVerificationError("Signature-Input is malformed")
    components = [(name, bool(req)) for name, req in re.findall(r'"([^"]+)"(;req)?', match.group(1))]
    params = {}
    for key, quoted, number in re.findall(r';([a-z]+)=(?:"([^"]*)"|(\d+))', match.group(2)):
        if key in params:
            raise SignatureVerificationError(f"Signature-Input repeats {key}")
        params[key] = quoted if number == "" else int(number)
    return components, serialized, params


def _component_value(name, is_request, method, url, status, headers):
    if is_request:
        parts = urlsplit(url)
        derived = {
            "@method": method.upper(),
            "@authority": parts.netloc.lower(),
            "@path": parts.path,
            "@query": f"?{parts.query}",
        }
        if name not in derived:
            raise SignatureVerificationError(f"unsupported request component {name}")
        return derived[name]
    if name == "@status":
        return str(status)
    value = headers.get(name)
    if value is None:
        raise SignatureVerificationError(f"response has no {name} header")
    return value.strip()


def _body_digest_matches(header, body):
    match = re.search(r"(?:^|,)\s*sha-256=:([A-Za-z0-9+/]+=*):", header or "")
    return match is not None and match.group(1) == base64.b64encode(hashlib.sha256(body).digest()).decode()


def verify_response_signature(method, url, status, headers, body, keys=None, now=None):
    """Raise SignatureVerificationError unless 0x signed exactly this response to exactly this request."""
    keys = trusted_signing_keys() if keys is None else keys
    now = time.time() if now is None else now
    signature_input = headers.get("signature-input")
    signature = headers.get("signature")
    if not signature_input or not signature:
        raise SignatureVerificationError("response is not signed")
    components, serialized, params = _parse_signature_input(signature_input)

    keyid = params.get("keyid")
    if keyid not in keys:
        raise SignatureVerificationError(f"unknown keyid {keyid}")
    if params.get("alg") != "ed25519":
        raise SignatureVerificationError(f"unexpected alg {params.get('alg')}")
    created = params.get("created")
    if not isinstance(created, int) or abs(now - created) > SIGNATURE_MAX_SKEW_SECONDS:
        raise SignatureVerificationError(f"signature created at {created} is not fresh")
    if "expires" in params and (not isinstance(params["expires"], int) or now > params["expires"]):
        raise SignatureVerificationError(f"signature expired at {params['expires']}")
    if headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise SignatureVerificationError(f"Content-Encoding {headers.get('content-encoding')} is not identity")
    covered = {f"{name};req" if is_request else name for name, is_request in components}
    for required in REQUIRED_SIGNATURE_COMPONENTS:
        if required not in covered:
            raise SignatureVerificationError(f"signature does not cover {required}")
    if not _body_digest_matches(headers.get("content-digest"), body):
        raise SignatureVerificationError("response body does not match Content-Digest")

    lines = [
        f'"{name}"{";req" if is_request else ""}: {_component_value(name, is_request, method, url, status, headers)}'
        for name, is_request in components
    ]
    lines.append(f'"@signature-params": {serialized}')
    match = re.fullmatch(r"sig=:([A-Za-z0-9+/]+=*):", signature.strip())
    if not match:
        raise SignatureVerificationError("Signature is malformed")
    try:
        keys[keyid].verify(base64.b64decode(match.group(1), validate=True), "\n".join(lines).encode())
    except (InvalidSignature, binascii.Error, ValueError):
        raise SignatureVerificationError("signature is invalid") from None
    return {"keyid": keyid, "created": created}


class ZeroExClient:
    def __init__(self, api_key, session=None, keys=None, sleep=time.sleep, clock=time.monotonic):
        self._api_key = api_key
        self._session = session or requests.Session()
        self._keys = keys
        self._sleep = sleep
        self._clock = clock
        self._throttle_lock = threading.Lock()
        self._last_request = float("-inf")

    def price(self, params):
        return self._get(PRICE_PATH, params)

    def quote(self, params):
        return self._get(QUOTE_PATH, params)

    def _throttle(self):
        with self._throttle_lock:
            wait = self._last_request + MIN_REQUEST_INTERVAL_SECONDS - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_request = self._clock()

    def _get(self, path, params):
        url = f"{ZEROX_API_URL}{path}?{urlencode(params)}"
        # identity keeps Content-Digest (computed over content-coded bytes) comparable to what we hash.
        headers = {"0x-api-key": self._api_key, "0x-version": "v2", "Accept-Signature": "sig=()",
                   "Accept-Encoding": "identity"}
        last_error = None
        retry_delay = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self._sleep(retry_delay if retry_delay is not None else RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
            retry_delay = None
            self._throttle()
            try:
                response = self._session.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
                                             allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_error = ZeroExError(f"0x request failed: {e}")
                logging.warning(f"0x {path} attempt {attempt + 1}/{MAX_ATTEMPTS} failed: {e}")
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = ZeroExError(f"0x HTTP {response.status_code}")
                retry_after = response.headers.get("retry-after", "")
                if retry_after.strip().isdigit():
                    retry_delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
                logging.warning(f"0x {path} attempt {attempt + 1}/{MAX_ATTEMPTS}: HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise ZeroExError(f"0x HTTP {response.status_code}: {response.content[:300]!r}")
            verify_response_signature("GET", url, response.status_code, response.headers, response.content, self._keys)
            try:
                return json.loads(response.content)
            except ValueError as e:
                raise ZeroExError(f"0x returned invalid JSON: {e}") from None
        raise last_error
