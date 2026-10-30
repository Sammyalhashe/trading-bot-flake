import logging
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executors.ethereum_executor as eth_mod
from executors import log_redaction
from executors.ethereum_executor import EthereumExecutor, retry_rpc_call

TEST_KEY = "0x" + "33" * 32
SECRET_URL = "https://user:tok3n@node.example.com/SECRETPATH"
TRUSTED_URL = "https://trusted.example.com/v2/TRUSTEDTOKEN?apikey=QUERYKEY99"
ZEROX_KEY = "zx-secret-api-key-123"


@pytest.fixture(autouse=True)
def clean_redaction_state(monkeypatch):
    monkeypatch.setattr(log_redaction, "_secrets", set())
    root = logging.getLogger()
    before = list(root.filters)
    yield
    root.filters[:] = before


def assert_no_secrets(caplog, *secrets):
    texts = [r.getMessage() for r in caplog.records] + [caplog.text]
    for text in texts:
        for secret in secrets:
            assert secret not in text


class TestUrlHost:
    @pytest.mark.parametrize("url, host", [
        (SECRET_URL, "node.example.com"),
        ("http://localhost:8545", "localhost"),
        ("https://mainnet.base.org", "mainnet.base.org"),
    ])
    def test_only_the_hostname_survives(self, url, host):
        assert log_redaction.url_host(url) == host

    @pytest.mark.parametrize("odd", ["", "node.example.com/SECRETPATH", "http://[bad", "::::", None, 5])
    def test_odd_inputs_do_not_raise_or_leak(self, odd):
        host = log_redaction.url_host(odd)

        assert "SECRETPATH" not in host


class TestRedact:
    def test_registered_url_parts_are_scrubbed_but_host_is_kept(self):
        log_redaction.register_url(SECRET_URL)

        scrubbed = log_redaction.redact(
            f"HTTPSConnectionPool(host='node.example.com'): Max retries exceeded with url: /SECRETPATH ({SECRET_URL})")

        assert "node.example.com" in scrubbed
        assert "SECRETPATH" not in scrubbed and "tok3n" not in scrubbed

    def test_query_values_and_path_segments_are_scrubbed(self):
        log_redaction.register_url(TRUSTED_URL)

        scrubbed = log_redaction.redact("failed url /v2/TRUSTEDTOKEN?apikey=QUERYKEY99")

        assert "TRUSTEDTOKEN" not in scrubbed and "QUERYKEY99" not in scrubbed

    def test_short_path_segments_are_not_treated_as_secrets(self):
        log_redaction.register_url("https://base.blockpi.network/v1/rpc/public")

        assert log_redaction.redact("rpc call to /v1/rpc/public") == "rpc call to /v1/rpc/public"

    def test_registered_secret_is_scrubbed_even_when_unrelated_to_a_url(self):
        log_redaction.register_secret(ZEROX_KEY)

        assert ZEROX_KEY not in log_redaction.redact(f"header 0x-api-key: {ZEROX_KEY}")

    def test_unparseable_url_does_not_raise(self):
        log_redaction.register_url("http://[bad")


class TestExecutorLogging:
    def test_constructor_logs_only_hostnames(self, caplog, monkeypatch):
        monkeypatch.setenv("ZEROX_API_KEY", ZEROX_KEY)
        caplog.set_level(logging.DEBUG)

        EthereumExecutor(SECRET_URL, TEST_KEY, "paper", trusted_rpc_url=TRUSTED_URL)

        assert "node.example.com" in caplog.text and "trusted.example.com" in caplog.text
        assert_no_secrets(caplog, "tok3n", "SECRETPATH", SECRET_URL, "TRUSTEDTOKEN", "QUERYKEY99",
                          TRUSTED_URL, ZEROX_KEY, TEST_KEY, "user:")

    def test_trusted_defaults_to_primary_and_is_still_hidden(self, caplog, monkeypatch):
        monkeypatch.delenv("BASE_TRUSTED_RPC_URL", raising=False)
        caplog.set_level(logging.DEBUG)

        EthereumExecutor(SECRET_URL, TEST_KEY, "paper")

        assert_no_secrets(caplog, "tok3n", "SECRETPATH", SECRET_URL)

    def test_exception_text_from_the_rpc_stack_is_scrubbed(self, caplog, monkeypatch):
        monkeypatch.setattr(eth_mod.time, "sleep", lambda s: None)
        EthereumExecutor(SECRET_URL, TEST_KEY, "paper")
        caplog.set_level(logging.DEBUG)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError(f"429 Too Many Requests for url: {SECRET_URL}")
            return "ok"

        assert retry_rpc_call(flaky) == "ok"

        assert "Too Many Requests" in caplog.text
        assert_no_secrets(caplog, "tok3n", "SECRETPATH")

    def test_secret_straddling_the_truncation_point_is_still_scrubbed(self, caplog, monkeypatch):
        monkeypatch.setattr(eth_mod.time, "sleep", lambda s: None)
        EthereumExecutor(SECRET_URL, TEST_KEY, "paper")
        caplog.set_level(logging.DEBUG)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("429 " + "x" * 90 + " SECRETPATH tail")
            return "ok"

        retry_rpc_call(flaky)

        assert_no_secrets(caplog, "SECRETPATH", "SECRET", "PATH tail")

    def test_failover_warning_is_scrubbed(self, caplog):
        executor = EthereumExecutor(SECRET_URL, TEST_KEY, "paper")
        caplog.set_level(logging.DEBUG)

        def refuse():
            raise ConnectionError(f"HTTPSConnectionPool(host=node.example.com): Max retries exceeded with url: /SECRETPATH ({SECRET_URL})")

        with pytest.raises(ConnectionError):
            executor._call_with_failover(refuse)

        assert "Rotating read RPC" in caplog.text
        assert_no_secrets(caplog, "tok3n", "SECRETPATH", SECRET_URL)

    def test_rotation_back_to_a_secret_primary_logs_only_its_host(self, caplog):
        executor = EthereumExecutor(SECRET_URL, TEST_KEY, "paper")
        caplog.set_level(logging.DEBUG)

        for _ in range(len(executor._rpc_endpoints)):
            executor._rotate_rpc()

        assert_no_secrets(caplog, "tok3n", "SECRETPATH", SECRET_URL)
