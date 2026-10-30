import base64
import hashlib
import json
import os
import sys
import time
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executors.zerox_client as zx
from executors.zerox_client import (
    SignatureVerificationError,
    ZeroExClient,
    ZeroExError,
    verify_response_signature,
)

KID = "test-key-1"
PRIVATE_KEY = Ed25519PrivateKey.generate()
KEYS = {KID: PRIVATE_KEY.public_key()}
URL = ("https://api.0x.org/swap/allowance-holder/quote?chainId=8453"
       "&sellToken=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913&sellAmount=1000000")
BODY = json.dumps({"liquidityAvailable": True, "buyAmount": "123"}).encode()
DEFAULT_COMPONENTS = ['"@status"', '"content-type"', '"content-digest"', '"@method";req',
                      '"@authority";req', '"@path";req', '"@query";req']


def _digest(body):
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


def signed_headers(body=BODY, url=URL, status=200, created=None, kid=KID, alg="ed25519",
                   components=DEFAULT_COMPONENTS, key=PRIVATE_KEY, extra_params=""):
    """Headers for a response signed the way the 0x API signs (RFC 9421, label `sig`)."""
    created = int(time.time()) if created is None else created
    headers = {"content-type": "application/json; charset=utf-8", "content-digest": _digest(body)}
    params = f'({" ".join(components)});created={created};keyid="{kid}";alg="{alg}";tag="0x-swap-api"{extra_params}'
    parts = requests.utils.urlparse(url)
    values = {
        '"@status"': str(status),
        '"content-type"': headers["content-type"],
        '"content-digest"': headers["content-digest"],
        '"@method";req': "GET",
        '"@authority";req': parts.netloc,
        '"@path";req': parts.path,
        '"@query";req': f"?{parts.query}",
    }
    base = "\n".join(f"{n}: {values[n]}" for n in components) + f'\n"@signature-params": {params}'
    signature = base64.b64encode(key.sign(base.encode())).decode()
    headers["signature-input"] = f"sig={params}"
    headers["signature"] = f"sig=:{signature}:"
    return requests.structures.CaseInsensitiveDict(headers)


def verify(headers, body=BODY, url=URL, status=200, now=None):
    return verify_response_signature("GET", url, status, headers, body, keys=KEYS, now=now)


class TestSignatureVerification:
    def test_valid_signature_accepted(self):
        result = verify(signed_headers())

        assert result["keyid"] == KID

    def test_pinned_production_key_matches_published_jwk(self):
        keys = zx.trusted_signing_keys()

        assert list(keys) == ["0x-signing-key-prod-24092026"]
        raw = keys["0x-signing-key-prod-24092026"].public_bytes_raw()
        assert base64.urlsafe_b64encode(raw).rstrip(b"=") == b"19P3C981JsyQsqwVho8Tlabx51rslvdruSH59mGuWn0"

    def test_signing_key_is_configurable(self, monkeypatch):
        x = base64.urlsafe_b64encode(PRIVATE_KEY.public_key().public_bytes_raw()).rstrip(b"=").decode()
        monkeypatch.setattr(zx, "SIGNING_KEY_ID", "rotated")
        monkeypatch.setattr(zx, "SIGNING_KEY_X", x)

        assert verify_response_signature("GET", URL, 200, signed_headers(kid="rotated"), BODY,
                                         keys=zx.trusted_signing_keys())["keyid"] == "rotated"

    def test_missing_signature_rejected(self):
        headers = signed_headers()
        del headers["signature"]

        with pytest.raises(SignatureVerificationError):
            verify(headers)

    def test_body_not_matching_digest_rejected(self):
        with pytest.raises(SignatureVerificationError, match="Content-Digest"):
            verify(signed_headers(), body=BODY.replace(b"123", b"999"))

    def test_digest_header_tampered_rejected(self):
        tampered = BODY.replace(b"123", b"999")
        headers = signed_headers()
        headers["content-digest"] = _digest(tampered)

        with pytest.raises(SignatureVerificationError):
            verify(headers, body=tampered)

    def test_unknown_keyid_rejected(self):
        with pytest.raises(SignatureVerificationError, match="keyid"):
            verify(signed_headers(kid="attacker-key"))

    def test_non_ed25519_alg_rejected(self):
        with pytest.raises(SignatureVerificationError, match="alg"):
            verify(signed_headers(alg="hmac-sha256"))

    def test_expired_signature_rejected(self):
        with pytest.raises(SignatureVerificationError, match="fresh"):
            verify(signed_headers(created=int(time.time()) - 61))

    def test_future_dated_signature_rejected(self):
        with pytest.raises(SignatureVerificationError, match="fresh"):
            verify(signed_headers(created=int(time.time()) + 61))

    def test_signature_must_cover_query(self):
        components = DEFAULT_COMPONENTS[:-1]

        with pytest.raises(SignatureVerificationError, match="@query;req"):
            verify(signed_headers(components=components))

    def test_response_for_a_different_request_rejected(self):
        headers = signed_headers()

        with pytest.raises(SignatureVerificationError, match="invalid"):
            verify(headers, url=URL.replace("sellAmount=1000000", "sellAmount=2000000"))

    def test_signature_by_untrusted_key_rejected(self):
        with pytest.raises(SignatureVerificationError, match="invalid"):
            verify(signed_headers(key=Ed25519PrivateKey.generate()))

    def test_status_is_bound(self):
        with pytest.raises(SignatureVerificationError):
            verify(signed_headers(status=200), status=203)

    def test_second_trusted_key_accepted_for_rotation(self, monkeypatch):
        x = base64.urlsafe_b64encode(PRIVATE_KEY.public_key().public_bytes_raw()).rstrip(b"=").decode()
        monkeypatch.setattr(zx, "SIGNING_KEY_ID", f"0x-signing-key-prod-24092026,next-key")
        monkeypatch.setattr(zx, "SIGNING_KEY_X", f"19P3C981JsyQsqwVho8Tlabx51rslvdruSH59mGuWn0,{x}")

        keys = zx.trusted_signing_keys()

        assert set(keys) == {"0x-signing-key-prod-24092026", "next-key"}
        assert verify_response_signature("GET", URL, 200, signed_headers(kid="next-key"), BODY, keys=keys)

    def test_mismatched_key_lists_rejected(self, monkeypatch):
        monkeypatch.setattr(zx, "SIGNING_KEY_ID", "a,b")
        monkeypatch.setattr(zx, "SIGNING_KEY_X", "19P3C981JsyQsqwVho8Tlabx51rslvdruSH59mGuWn0")

        with pytest.raises(ValueError):
            zx.trusted_signing_keys()

    @pytest.mark.parametrize("dropped", ['"@status"', '"content-digest"', '"@authority";req', '"@path";req',
                                         '"@method";req', '"content-type"'])
    def test_each_required_component_must_be_covered(self, dropped):
        components = [c for c in DEFAULT_COMPONENTS if c != dropped]

        with pytest.raises(SignatureVerificationError, match="does not cover"):
            verify(signed_headers(components=components))

    def test_clock_skew_boundary(self):
        now = 1_800_000_000

        assert verify(signed_headers(created=now - 60), now=now)
        assert verify(signed_headers(created=now + 60), now=now)
        with pytest.raises(SignatureVerificationError, match="fresh"):
            verify(signed_headers(created=now - 61), now=now)

    def test_expired_expires_param_rejected(self):
        now = int(time.time())

        assert verify(signed_headers(extra_params=f";expires={now + 30}"), now=now)
        with pytest.raises(SignatureVerificationError, match="expired"):
            verify(signed_headers(extra_params=f";expires={now - 1}"), now=now)

    @pytest.mark.parametrize("signature", ["sig=:!!!notbase64:", "sig=:abc:", "sig=:" + "A" * 20 + ":",
                                           "sig=:YWJj:, sig=:YWJj:", "", "other=:YWJj:"])
    def test_malformed_signature_header_fails_closed(self, signature):
        headers = signed_headers()
        headers["signature"] = signature

        with pytest.raises(SignatureVerificationError):
            verify(headers)

    @pytest.mark.parametrize("mangle", [
        lambda v: v[:25],
        lambda v: v + ', sig=("@status");created=1;keyid="test-key-1";alg="ed25519"',
        lambda v: v + ';keyid="attacker-key"',
        lambda v: v.replace("sig=", "zig="),
    ])
    def test_malformed_signature_input_fails_closed(self, mangle):
        headers = signed_headers()
        headers["signature-input"] = mangle(headers["signature-input"])

        with pytest.raises(SignatureVerificationError):
            verify(headers)


def _response(status, body=BODY, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = headers if headers is not None else requests.structures.CaseInsensitiveDict()
    return resp


class FakeSession:
    """Serves queued responses; each item is a status code, a response, or an exception."""
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout, "allow_redirects": allow_redirects})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            if item == 200:
                return _response(200, headers=signed_headers(url=url))
            return _response(item, body=b'{"name":"ERR"}')
        return item


PARAMS = {"chainId": 8453, "sellToken": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "sellAmount": 1000000}


def client(session):
    return ZeroExClient("test-api-key", session=session, keys=KEYS, sleep=lambda s: None)


class TestClientHttp:
    def test_request_shape_and_signed_body_returned(self):
        session = FakeSession(200)

        result = client(session).quote(PARAMS)

        assert result == json.loads(BODY)
        call = session.calls[0]
        assert call["url"].startswith("https://api.0x.org/swap/allowance-holder/quote?")
        assert "sellAmount=1000000" in call["url"]
        assert call["headers"]["0x-api-key"] == "test-api-key"
        assert call["headers"]["0x-version"] == "v2"
        assert "Accept-Signature" in call["headers"]
        assert call["headers"]["Accept-Encoding"] == "identity"
        assert call["allow_redirects"] is False
        assert call["timeout"] is not None

    def test_price_uses_price_endpoint(self):
        session = FakeSession(200)

        client(session).price(PARAMS)

        assert "/swap/allowance-holder/price?" in session.calls[0]["url"]

    def test_429_is_retried_then_succeeds(self):
        session = FakeSession(429, 200)

        assert client(session).quote(PARAMS)["buyAmount"] == "123"
        assert len(session.calls) == 2

    def test_persistent_5xx_fails_closed_after_bounded_retries(self):
        session = FakeSession(500, 502, 503, 200)

        with pytest.raises(ZeroExError):
            client(session).quote(PARAMS)
        assert len(session.calls) == zx.MAX_ATTEMPTS

    def test_timeouts_fail_closed(self):
        session = FakeSession(*[requests.Timeout("read timed out")] * zx.MAX_ATTEMPTS)

        with pytest.raises(ZeroExError):
            client(session).quote(PARAMS)

    def test_4xx_is_not_retried(self):
        session = FakeSession(400, 200)

        with pytest.raises(ZeroExError, match="400"):
            client(session).quote(PARAMS)
        assert len(session.calls) == 1

    def test_unsigned_200_rejected(self):
        session = FakeSession(_response(200))

        with pytest.raises(SignatureVerificationError):
            client(session).quote(PARAMS)

    def test_persistent_429_fails_closed(self):
        session = FakeSession(*[429] * zx.MAX_ATTEMPTS)

        with pytest.raises(ZeroExError, match="429"):
            client(session).quote(PARAMS)
        assert len(session.calls) == zx.MAX_ATTEMPTS

    def test_connection_errors_fail_closed(self):
        session = FakeSession(*[requests.ConnectionError("refused")] * zx.MAX_ATTEMPTS)

        with pytest.raises(ZeroExError):
            client(session).quote(PARAMS)

    def test_unsigned_price_rejected(self):
        with pytest.raises(SignatureVerificationError):
            client(FakeSession(_response(200))).price(PARAMS)

    def test_non_200_body_is_never_returned(self):
        session = FakeSession(_response(404, body=b'{"buyAmount":"999"}'))

        with pytest.raises(ZeroExError, match="404"):
            client(session).quote(PARAMS)

    def test_redirect_is_an_error_not_followed(self):
        session = FakeSession(_response(302, headers=requests.structures.CaseInsensitiveDict(
            {"location": "https://evil.example/steal"})))

        with pytest.raises(ZeroExError, match="302"):
            client(session).quote(PARAMS)
        assert len(session.calls) == 1

    def test_content_coded_response_rejected(self):
        def coded(url):
            headers = signed_headers(url=url)
            headers["content-encoding"] = "gzip"
            return _response(200, headers=headers)

        class CodingSession(FakeSession):
            def get(self, url, **kwargs):
                super().get(url, **kwargs)
                return coded(url)

        with pytest.raises(SignatureVerificationError, match="Content-Encoding"):
            client(CodingSession(200)).quote(PARAMS)

    @pytest.mark.parametrize("retry_after, expected", [("2", 2.0), ("100", zx.MAX_RETRY_AFTER_SECONDS)])
    def test_retry_after_honored_with_cap(self, retry_after, expected):
        sleeps = []
        throttled = _response(429, headers=requests.structures.CaseInsensitiveDict({"retry-after": retry_after}))
        c = ZeroExClient("k", session=FakeSession(throttled, 200), keys=KEYS, sleep=sleeps.append)

        c.quote(PARAMS)

        assert expected in sleeps

    def test_requests_are_throttled(self):
        sleeps = []
        clock = iter([100.0, 100.0, 100.05, 100.05]).__next__
        c = ZeroExClient("k", session=FakeSession(200, 200), keys=KEYS, sleep=sleeps.append, clock=clock)

        c.quote(PARAMS)
        c.quote(PARAMS)

        assert sleeps and sleeps[0] == pytest.approx(zx.MIN_REQUEST_INTERVAL_SECONDS - 0.05)
