"""EthereumExecutor with ONCHAIN_ROUTER=zerox. Every 0x and trusted-RPC call is mocked; no network."""
import base64
import copy
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from eth_abi import decode, encode
from eth_account import Account
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import ContractLogicError, TransactionNotFound

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(REPO)

import executors.ethereum_executor as eth_mod
from executors.ethereum_executor import (
    ALLOWANCE_HOLDER_ADDRESS,
    SETTLER_REGISTRY_ADDRESS,
    SLIPPAGE_BPS,
    TOKENS,
    EthereumExecutor,
)
from executors.zerox_client import SignatureVerificationError, ZeroExError

TEST_KEY = "0x" + "22" * 32
WALLET = Account.from_key(TEST_KEY).address
STRANGER = "0x1111111111111111111111111111111111111111"
USDC = TOKENS["USDC"]
WETH = TOKENS["WETH"]
AERO = TOKENS["AERO"]
WBTC = "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c"
SETTLER = "0x103502909141F7025a94B7b99A276DcD128E21a8"       # registry ownerOf(2) on Base, read live
PREV_SETTLER = "0x7D6839415693801E94cdC4A7d732C9a3fdbBd1D7"  # registry prev(2) on Base, read live
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
AERODROME_ROUTER = Web3.to_checksum_address(eth_mod.AERODROME_ROUTER_ADDRESS)
BLOCK_NUMBER = 38_000_000
SWAP_BLOCK = BLOCK_NUMBER + 1
BLOCK_TS = 1_800_000_000
BASE_FEE = 5_000_000
PRIORITY_FEE = 1_000_000
GAS_ESTIMATE = 250_000
EXEC_SELECTOR = "0x2213bc0b"     # AllowanceHolder.exec(address,address,uint256,address,bytes)
EXECUTE_SELECTOR = "0x1fff991f"  # Settler.execute((address,address,uint256),bytes[],bytes32)
BUY_IN, BUY_OUT = 100 * 10**6, 4 * 10**16       # 100 USDC -> 0.04 WETH ($2500)
SELL_IN, SELL_OUT = 10**18, 2490 * 10**6        # 1 WETH -> 2490 USDC


def floor(amount):
    return amount * (10_000 - SLIPPAGE_BPS) // 10_000


def settler_execute(recipient, buy_token, min_out, selector=EXECUTE_SELECTOR):
    args = encode(["(address,address,uint256)", "bytes[]", "bytes32"],
                  [(recipient, buy_token, min_out), [b"\x38\xc9\xc1\x47" + b"\x00" * 64], b"\x00" * 32])
    return bytes.fromhex(selector[2:]) + args


def exec_calldata(token, amount, inner, target=SETTLER, operator=None):
    args = encode(["address", "address", "uint256", "address", "bytes"],
                  [operator or target, token, amount, target, inner])
    return EXEC_SELECTOR + args.hex()


def make_quote(sell_token, buy_token, sell_amount, buy_amount, firm=True):
    """Shaped like a Base /swap/allowance-holder/quote (or /price) 200 response."""
    min_buy = floor(buy_amount)
    quote = {
        "allowanceTarget": ALLOWANCE_HOLDER_ADDRESS.lower(),
        "blockNumber": str(BLOCK_NUMBER),
        "buyAmount": str(buy_amount),
        "buyToken": buy_token.lower(),
        "fees": {
            "integratorFee": None,
            "zeroExFee": {"amount": str(buy_amount * 15 // 10_000), "token": buy_token.lower(), "type": "volume"},
            "gasFee": None,
        },
        "issues": {"allowance": None, "balance": None, "simulationIncomplete": False, "invalidSourcesPassed": []},
        "liquidityAvailable": True,
        "minBuyAmount": str(min_buy),
        "route": {
            "fills": [{"from": sell_token.lower(), "to": buy_token.lower(), "source": "Aerodrome_V3", "proportionBps": "10000"}],
            "tokens": [{"address": sell_token.lower(), "symbol": "IN"}, {"address": buy_token.lower(), "symbol": "OUT"}],
        },
        "sellAmount": str(sell_amount),
        "sellToken": sell_token.lower(),
        "tokenMetadata": {"buyToken": {"buyTaxBps": "0", "sellTaxBps": "0"},
                          "sellToken": {"buyTaxBps": "0", "sellTaxBps": "0"}},
        "totalNetworkFee": "1394929353342",
        "zid": "0x9c2d3156ecfc7dd6b4acc463",
    }
    if firm:
        quote["mode"] = "exact-in"
        quote["transaction"] = {
            "to": ALLOWANCE_HOLDER_ADDRESS.lower(),
            "data": exec_calldata(Web3.to_checksum_address(sell_token), sell_amount,
                                  settler_execute(WALLET, Web3.to_checksum_address(buy_token), min_buy)),
            "gas": "300000",
            "gasPrice": "6000000",
            "value": "0",
        }
    return quote


def with_changes(quote, changes):
    """Set nested keys given as {"a__b": value}; a value of DELETE removes the key."""
    quote = copy.deepcopy(quote)
    for path, value in changes.items():
        target = quote
        *parents, leaf = path.split("__")
        for p in parents:
            target = target[p]
        if value is DELETE:
            del target[leaf]
        else:
            target[leaf] = value
    return quote


DELETE = object()


class FakeZeroEx:
    """Stands in for ZeroExClient. Responses are dicts, exceptions, or callables of params."""
    def __init__(self, price=None, quote=None):
        self.responses = {"price": price, "quote": quote}
        self.calls = []

    def _serve(self, kind, params):
        self.calls.append((kind, dict(params)))
        response = self.responses[kind]
        if callable(response):
            response = response(params)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)

    def price(self, params):
        return self._serve("price", params)

    def quote(self, params):
        return self._serve("quote", params)


class Chain:
    """Mocked Base state behind the trusted RPC."""
    def __init__(self):
        self.registry = MagicMock()
        self.registry.functions.ownerOf.return_value.call.return_value = SETTLER
        self.registry.functions.prev.return_value.call.return_value = PREV_SETTLER
        self.tokens = {}

    def token(self, address, allowance=2**256 - 1, balances=None):
        """balances maps block number -> our balance (default: 0 before the swap block, plenty after)."""
        balances = balances if balances is not None else {BLOCK_NUMBER: 0, SWAP_BLOCK: 10**30}
        token = MagicMock()
        token.functions.allowance.return_value.call.return_value = allowance
        token.functions.balanceOf.return_value.call.side_effect = lambda block_identifier: balances[block_identifier]
        approve_fn = token.functions.approve.return_value
        approve_fn.estimate_gas.return_value = 50_000
        approve_fn.build_transaction.side_effect = lambda params: {
            **params, "to": Web3.to_checksum_address(address), "data": "0x095ea7b3"}
        self.tokens[address.lower()] = token
        return token

    def contract(self, address, abi):
        if address.lower() == SETTLER_REGISTRY_ADDRESS.lower():
            return self.registry
        return self.tokens[address.lower()]


@pytest.fixture
def nonces():
    return {"pending": 7, "latest": 7}


@pytest.fixture
def chain():
    c = Chain()
    for token in (USDC, WETH, AERO):
        c.token(token)
    return c


@pytest.fixture
def signed():
    return []


def _receipt(status=1):
    return AttributeDict({"status": status, "gasUsed": 180_000, "blockNumber": SWAP_BLOCK})


@pytest.fixture
def executor(monkeypatch, tmp_path, nonces, chain, signed):
    monkeypatch.delenv("ONCHAIN_KILL_SWITCH", raising=False)
    monkeypatch.setattr(eth_mod, "ONCHAIN_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    ex = EthereumExecutor("http://read.invalid", TEST_KEY, "live", trusted_rpc_url="http://trusted.invalid")
    ex.onchain_router = "zerox"
    ex.zerox = FakeZeroEx()

    ex.trusted_w3 = MagicMock()
    eth = ex.trusted_w3.eth
    eth.chain_id = 8453
    eth.get_block.return_value = {"timestamp": BLOCK_TS, "baseFeePerGas": BASE_FEE, "number": BLOCK_NUMBER}
    eth.max_priority_fee = PRIORITY_FEE
    eth.get_balance.return_value = 10**18
    eth.get_code.return_value = b"\x36\x3d\x3d\x37"
    eth.estimate_gas.return_value = GAS_ESTIMATE
    eth.get_transaction_count.side_effect = lambda addr, block: nonces[block]
    eth.send_raw_transaction.side_effect = lambda raw: Web3.keccak(raw)
    eth.wait_for_transaction_receipt.return_value = _receipt()
    eth.get_transaction_receipt.return_value = _receipt()
    eth.contract.side_effect = chain.contract

    real_sign = ex.account.sign_transaction
    ex.account.sign_transaction = lambda tx: (signed.append(tx), real_sign(tx))[1]
    return ex


def _quotes(executor, sell, buy, amount_in, buy_amount, price=None, quote=None):
    executor.zerox = FakeZeroEx(
        price=price if price is not None else make_quote(sell, buy, amount_in, buy_amount, firm=False),
        quote=quote if quote is not None else make_quote(sell, buy, amount_in, buy_amount),
    )
    return executor.zerox


def _buy_quotes(executor, **kwargs):
    return _quotes(executor, USDC, WETH, BUY_IN, BUY_OUT, **kwargs)


def _sell_quotes(executor, **kwargs):
    return _quotes(executor, WETH, USDC, SELL_IN, SELL_OUT, **kwargs)


def _buy(executor, reference_price=2500, recipient=None):
    return executor.execute_swap(USDC, WETH, BUY_IN, recipient or executor.account.address, reference_price=reference_price)


def _sell(executor, reference_price=2500):
    return executor.execute_swap(WETH, USDC, SELL_IN, executor.account.address, reference_price=reference_price)


def _assert_nothing_signed(executor, signed):
    assert signed == []
    executor.trusted_w3.eth.send_raw_transaction.assert_not_called()


def _install_aerodrome(executor, amount_out):
    router = MagicMock()

    def get_amounts_out(amount_in, routes):
        call = MagicMock()
        call.call.return_value = [amount_in] + [amount_out] * len(routes)
        return call
    router.functions.getAmountsOut.side_effect = get_amounts_out
    swap_fn = router.functions.swapExactTokensForTokens.return_value
    swap_fn.estimate_gas.return_value = 200_000
    swap_fn.build_transaction.side_effect = lambda params: {**params, "to": AERODROME_ROUTER, "data": "0xcac88ea9"}
    executor.router_contract = router
    return router


def rejections(sell, buy, amount_in, buy_amount):
    """Quote mutations that must be refused, with the refusal reason they must produce."""
    sell_cs, buy_cs = Web3.to_checksum_address(sell), Web3.to_checksum_address(buy)
    good_inner = settler_execute(WALLET, buy_cs, floor(buy_amount))
    data = lambda **kw: exec_calldata(kw.pop("token", sell_cs), kw.pop("amount", amount_in),
                                      kw.pop("inner", good_inner), **kw)
    return {
        "wrong_to": ({"transaction__to": SETTLER}, "transaction.to"),
        "nonzero_value": ({"transaction__value": "1"}, "transaction.value"),
        "allowance_target_mismatch": ({"allowanceTarget": PERMIT2}, "allowanceTarget"),
        "issues_spender_mismatch": ({"issues__allowance": {"actual": "0", "spender": SETTLER}}, "allowance spender"),
        "no_liquidity": ({"liquidityAvailable": False}, "no liquidity"),
        "simulation_incomplete": ({"issues__simulationIncomplete": True}, "could not simulate"),
        "insufficient_balance": ({"issues__balance": {"token": sell, "actual": "0", "expected": str(amount_in)}}, "balance"),
        "invalid_sources": ({"issues__invalidSourcesPassed": ["Nope"]}, "invalid sources"),
        "zero_buy_amount": ({"buyAmount": "0", "minBuyAmount": "0"}, "non-positive"),
        "min_below_our_floor": ({"minBuyAmount": str(floor(buy_amount) - 1)}, "below our floor"),
        "stale_quote": ({"blockNumber": str(BLOCK_NUMBER - 10)}, "blocks ago"),
        "echo_sell_amount_mismatch": ({"sellAmount": str(amount_in // 2)}, "does not match the requested order"),
        "echo_buy_token_mismatch": ({"buyToken": AERO.lower()}, "does not match the requested order"),
        "exec_token_mismatch": ({"transaction__data": data(token=STRANGER)}, "exec() token/amount"),
        "exec_amount_above_sell_amount": ({"transaction__data": data(amount=amount_in + 1)}, "exec() token/amount"),
        "exec_target_not_settler": ({"transaction__data": data(target=PERMIT2)}, "neither Settler"),
        "exec_operator_not_target": ({"transaction__data": data(operator=PERMIT2)}, "operator"),
        "not_exec_selector": ({"transaction__data": "0xdeadbeef" + data()[10:]}, "not AllowanceHolder.exec"),
        "settler_not_execute": ({"transaction__data": data(inner=settler_execute(WALLET, buy_cs, floor(buy_amount), selector="0xdeadbeef"))}, "not Settler.execute"),
        "settler_foreign_recipient": ({"transaction__data": data(inner=settler_execute(STRANGER, buy_cs, floor(buy_amount)))}, "recipient"),
        "settler_wrong_buy_token": ({"transaction__data": data(inner=settler_execute(WALLET, AERO, floor(buy_amount)))}, "buyToken"),
        "settler_min_out_below_floor": ({"transaction__data": data(inner=settler_execute(WALLET, buy_cs, floor(buy_amount) - 1))}, "minAmountOut"),
        "settler_undecodable": ({"transaction__data": data(inner=bytes.fromhex(EXECUTE_SELECTOR[2:]) + b"\x01\x02")}, "malformed"),
        "missing_transaction": ({"transaction": DELETE}, "malformed"),
        "missing_issues": ({"issues": DELETE}, "malformed"),
    }


BUY_REJECTIONS = rejections(USDC, WETH, BUY_IN, BUY_OUT)
SELL_REJECTIONS = rejections(WETH, USDC, SELL_IN, SELL_OUT)


class TestHappyPath:
    def test_buy_signs_0x_calldata_with_our_fee_nonce_and_gas(self, executor, signed):
        zerox = _buy_quotes(executor)

        result = _buy(executor)

        assert result["success"] is True and result["confirmed"] is True
        assert [kind for kind, _ in zerox.calls] == ["price", "quote"]
        tx = signed[-1]
        assert tx["to"] == ALLOWANCE_HOLDER_ADDRESS
        assert tx["data"] == zerox.responses["quote"]["transaction"]["data"]
        assert tx["value"] == 0
        assert tx["nonce"] == 7
        assert tx["chainId"] == 8453
        assert tx["gas"] == GAS_ESTIMATE * 125 // 100
        assert tx["maxFeePerGas"] == 2 * BASE_FEE + PRIORITY_FEE
        assert tx["maxPriorityFeePerGas"] == PRIORITY_FEE
        assert "gasPrice" not in tx
        executor.trusted_w3.eth.send_raw_transaction.assert_called_once()

    def test_quote_request_pins_taker_recipient_and_slippage_ppm(self, executor):
        zerox = _buy_quotes(executor)

        _buy(executor)

        for _, params in zerox.calls:
            assert params["chainId"] == 8453
            assert params["sellToken"] == USDC and params["buyToken"] == WETH
            assert int(params["sellAmount"]) == BUY_IN
            assert params["taker"] == params["recipient"] == executor.account.address
            assert params["slippagePpm"] == SLIPPAGE_BPS * 100
            assert "slippageBps" not in params

    def test_sell_happy_path(self, executor, signed):
        _sell_quotes(executor)

        result = _sell(executor)

        assert result["success"] is True and result["confirmed"] is True
        assert signed[-1]["to"] == ALLOWANCE_HOLDER_ADDRESS

    def test_settler_in_dwell_period_accepted_via_prev(self, executor):
        inner = settler_execute(WALLET, WETH, floor(BUY_OUT))
        quote = with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT),
                             {"transaction__data": exec_calldata(USDC, BUY_IN, inner, target=PREV_SETTLER)})
        _buy_quotes(executor, quote=quote)

        assert _buy(executor)["success"] is True

    def test_logs_observed_zerox_fee(self, executor, caplog):
        _buy_quotes(executor)

        with caplog.at_level(logging.INFO):
            _buy(executor)

        assert "zeroExFee" in caplog.text

    @pytest.mark.parametrize("changes", [{"fees": DELETE}, {"fees": None}, {"fees__zeroExFee": None}, {"route": None}])
    def test_missing_fee_or_route_metadata_does_not_crash(self, executor, changes):
        _buy_quotes(executor, quote=with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT), changes))

        assert _buy(executor)["success"] is True

    @pytest.mark.parametrize("to", [ALLOWANCE_HOLDER_ADDRESS, ALLOWANCE_HOLDER_ADDRESS.lower(), "0x0000000000001FF3684F28C67538D4D072C22734"])
    def test_to_matches_regardless_of_case(self, executor, to):
        _buy_quotes(executor, quote=with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT), {"transaction__to": to}))

        assert _buy(executor)["success"] is True

    def test_exec_amount_below_sell_amount_accepted(self, executor):
        inner = settler_execute(WALLET, WETH, floor(BUY_OUT))
        _buy_quotes(executor, quote=with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT),
                                                 {"transaction__data": exec_calldata(USDC, BUY_IN - 1, inner)}))

        assert _buy(executor)["success"] is True

    def test_min_buy_exactly_at_floor_accepted(self, executor):
        quote = make_quote(USDC, WETH, BUY_IN, BUY_OUT)
        assert int(quote["minBuyAmount"]) == floor(BUY_OUT)
        _buy_quotes(executor, quote=quote)

        assert _buy(executor)["success"] is True

    @pytest.mark.parametrize("age, ok", [(eth_mod.ZEROX_MAX_QUOTE_AGE_BLOCKS, True), (eth_mod.ZEROX_MAX_QUOTE_AGE_BLOCKS + 1, False)])
    def test_quote_age_boundary(self, executor, signed, age, ok):
        _buy_quotes(executor, quote=with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT),
                                                 {"blockNumber": str(BLOCK_NUMBER - age)}))

        result = _buy(executor)

        assert result["success"] is ok
        if not ok:
            assert "blocks ago" in result["error"]
            _assert_nothing_signed(executor, signed)

    def test_executor_exposes_onchain_round_trip_fee(self, executor):
        assert executor.round_trip_fee_pct == Decimal("0.006")


class TestQuoteValidation:
    @pytest.mark.parametrize("case", sorted(BUY_REJECTIONS))
    def test_invalid_quote_refused_and_buy_never_falls_back(self, executor, signed, case):
        changes, reason = BUY_REJECTIONS[case]
        _buy_quotes(executor, quote=with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT), changes))
        router = _install_aerodrome(executor, BUY_OUT)

        result = _buy(executor)

        assert result["success"] is False
        assert result["error"].startswith("0x: ") and reason in result["error"]
        _assert_nothing_signed(executor, signed)
        router.functions.getAmountsOut.assert_not_called()

    def test_indicative_price_without_liquidity_refused_before_approval(self, executor, chain, signed):
        price = with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT, firm=False), {"liquidityAvailable": False})
        zerox = _buy_quotes(executor, price=price)
        chain.tokens[USDC.lower()].functions.allowance.return_value.call.return_value = 0

        assert _buy(executor)["success"] is False
        assert [kind for kind, _ in zerox.calls] == ["price"]
        chain.tokens[USDC.lower()].functions.approve.assert_not_called()
        _assert_nothing_signed(executor, signed)

    def test_reference_breach_on_price_step_stops_before_quote(self, executor, signed):
        price = make_quote(USDC, WETH, BUY_IN, BUY_OUT * 90 // 100, firm=False)
        zerox = _buy_quotes(executor, price=price)

        result = _buy(executor)

        assert result["success"] is False and "reference" in result["error"]
        assert [kind for kind, _ in zerox.calls] == ["price"]
        _assert_nothing_signed(executor, signed)

    def test_settler_registry_revert_means_paused(self, executor, chain, signed):
        _buy_quotes(executor)
        chain.registry.functions.ownerOf.return_value.call.side_effect = ContractLogicError("execution reverted")

        result = _buy(executor)

        assert result["success"] is False and "paused" in result["error"]
        _assert_nothing_signed(executor, signed)

    def test_prev_not_consulted_when_owner_matches(self, executor, chain):
        _buy_quotes(executor)

        assert _buy(executor)["success"] is True
        chain.registry.functions.ownerOf.assert_called_with(2)
        chain.registry.functions.prev.assert_not_called()

    def test_prev_revert_during_dwell_check_refused(self, executor, chain, signed):
        inner = settler_execute(WALLET, WETH, floor(BUY_OUT))
        quote = with_changes(make_quote(USDC, WETH, BUY_IN, BUY_OUT),
                             {"transaction__data": exec_calldata(USDC, BUY_IN, inner, target=PREV_SETTLER)})
        _buy_quotes(executor, quote=quote)
        chain.registry.functions.prev.return_value.call.side_effect = ContractLogicError("execution reverted")

        assert _buy(executor)["success"] is False
        _assert_nothing_signed(executor, signed)

    def test_allowance_holder_without_code_refused(self, executor, signed):
        _buy_quotes(executor)
        executor.trusted_w3.eth.get_code.return_value = b""

        assert _buy(executor)["success"] is False
        _assert_nothing_signed(executor, signed)

    def test_wrong_chain_refused(self, executor, signed):
        zerox = _buy_quotes(executor)
        executor.trusted_w3.eth.chain_id = 1

        assert _buy(executor)["success"] is False
        assert zerox.calls == []
        _assert_nothing_signed(executor, signed)

    def test_reference_price_breach_refused(self, executor, signed):
        _buy_quotes(executor)

        result = _buy(executor, reference_price=2400)

        assert result["success"] is False and "reference" in result["error"]
        _assert_nothing_signed(executor, signed)

    @pytest.mark.parametrize("fill, ok", [(2450 * 10**6, True), (2450 * 10**6 - 1, False), (2600 * 10**6, True)])
    def test_sell_reference_boundary_and_direction(self, executor, fill, ok):
        _quotes(executor, WETH, USDC, SELL_IN, fill)
        _install_aerodrome(executor, 0)

        assert _sell(executor, reference_price=2500)["success"] is ok

    @pytest.mark.parametrize("reference", [None, 0, -1])
    def test_missing_or_nonpositive_reference_price_refused(self, executor, signed, reference):
        zerox = _buy_quotes(executor)

        result = _buy(executor, reference_price=reference)

        assert result["success"] is False and "reference" in result["error"]
        assert zerox.calls == []
        _assert_nothing_signed(executor, signed)

    def test_simulation_revert_aborts_before_signing(self, executor, signed):
        _buy_quotes(executor)
        executor.trusted_w3.eth.estimate_gas.side_effect = ContractLogicError("execution reverted")

        assert _buy(executor)["success"] is False
        _assert_nothing_signed(executor, signed)

    def test_tokens_outside_registry_hard_fail(self, executor, signed):
        router = _install_aerodrome(executor, 10**6)

        result = executor.execute_swap(STRANGER, USDC, 10**18, executor.account.address, reference_price=1)

        assert result["success"] is False and "registry" in result["error"]
        assert executor.zerox.calls == []
        router.functions.getAmountsOut.assert_not_called()
        _assert_nothing_signed(executor, signed)


class TestRecipientAndInFlight:
    def test_foreign_recipient_refused_before_any_0x_call(self, executor, signed):
        zerox = _buy_quotes(executor)

        result = _buy(executor, recipient=STRANGER)

        assert result["success"] is False and "own wallet" in result["error"]
        assert zerox.calls == []
        _assert_nothing_signed(executor, signed)

    @pytest.mark.parametrize("form", [str.lower, Web3.to_checksum_address])
    def test_own_address_accepted_in_any_case(self, executor, form):
        _buy_quotes(executor)

        assert _buy(executor, recipient=form(executor.account.address))["success"] is True

    def test_pending_nonce_ahead_refuses_before_any_0x_call(self, executor, signed, nonces):
        nonces["pending"], nonces["latest"] = 8, 7
        zerox = _buy_quotes(executor)

        result = _buy(executor)

        assert result["success"] is False and "in flight" in result["error"]
        assert zerox.calls == []
        _assert_nothing_signed(executor, signed)

    def test_last_sent_tx_without_receipt_refuses(self, executor, signed):
        executor._last_sent_hash = b"\x01" * 32
        executor.trusted_w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("no receipt")
        zerox = _buy_quotes(executor)

        result = _buy(executor)

        assert result["success"] is False and "in flight" in result["error"]
        assert zerox.calls == []
        _assert_nothing_signed(executor, signed)

    def test_concurrent_buys_send_exactly_one_transaction(self, executor, nonces):
        first_in_price, second_in_price = threading.Event(), threading.Event()
        order = itertools.count()

        def price(params):
            if next(order) == 0:
                first_in_price.set()
                second_in_price.wait(timeout=1)
            else:
                second_in_price.set()
            return make_quote(USDC, WETH, BUY_IN, BUY_OUT, firm=False)
        _buy_quotes(executor, price=price)

        def broadcast(raw):
            nonces["pending"] += 1
            return Web3.keccak(raw)
        executor.trusted_w3.eth.send_raw_transaction.side_effect = broadcast
        results = []
        first = threading.Thread(target=lambda: results.append(_buy(executor)))
        second = threading.Thread(target=lambda: results.append(_buy(executor)))

        first.start()
        assert first_in_price.wait(timeout=5)
        second.start()
        first.join()
        second.join()

        assert executor.trusted_w3.eth.send_raw_transaction.call_count == 1
        assert sorted(r["success"] for r in results) == [False, True]


class TestOutageFailsClosed:
    @pytest.mark.parametrize("error", [
        ZeroExError("0x request timed out"),
        ZeroExError("0x HTTP 503"),
        ZeroExError("0x HTTP 429"),
        SignatureVerificationError("signature is invalid"),
        SignatureVerificationError("response is not signed"),
        SignatureVerificationError("unknown keyid evil"),
        SignatureVerificationError("signature created at 1 is not fresh"),
        SignatureVerificationError("response body does not match Content-Digest"),
    ])
    def test_buy_refused_without_fallback(self, executor, signed, error):
        executor.zerox = FakeZeroEx(price=error, quote=error)
        router = _install_aerodrome(executor, BUY_OUT)

        result = _buy(executor)

        assert result["success"] is False and str(error) in result["error"]
        _assert_nothing_signed(executor, signed)
        router.functions.getAmountsOut.assert_not_called()

    def test_missing_api_key_means_no_zerox_buys(self, executor, signed):
        executor.zerox = None

        assert _buy(executor)["success"] is False
        _assert_nothing_signed(executor, signed)

    def test_kill_switch_engaged_after_quote_blocks_signing(self, executor, signed, tmp_path):
        quote = make_quote(USDC, WETH, BUY_IN, BUY_OUT)

        def engage_then_quote(params):
            (tmp_path / "kill").touch()
            return quote
        _buy_quotes(executor, quote=engage_then_quote)

        result = _buy(executor)

        assert result == {"success": False, "error": "kill switch engaged"}
        _assert_nothing_signed(executor, signed)


class TestApproval:
    def test_exact_amount_to_allowance_holder_then_fresh_quote(self, executor, chain, signed):
        usdc = chain.tokens[USDC.lower()]
        usdc.functions.allowance.return_value.call.return_value = 0
        order = []
        usdc.functions.approve.side_effect = lambda spender, amount: (order.append("approve"), usdc.functions.approve.return_value)[1]
        quote = make_quote(USDC, WETH, BUY_IN, BUY_OUT)
        _buy_quotes(executor, quote=lambda params: (order.append("quote"), quote)[1])

        assert _buy(executor)["success"] is True

        usdc.functions.approve.assert_called_once_with(ALLOWANCE_HOLDER_ADDRESS, BUY_IN)
        assert order == ["approve", "quote"]
        assert [tx["nonce"] for tx in signed] == [7, 8]
        assert {c.args[1] for c in usdc.functions.allowance.call_args_list} == {ALLOWANCE_HOLDER_ADDRESS}

    def test_nonzero_allowance_reset_to_zero_first(self, executor, chain):
        usdc = chain.tokens[USDC.lower()]
        usdc.functions.allowance.return_value.call.return_value = 5
        _buy_quotes(executor)

        assert _buy(executor)["success"] is True

        assert [c.args for c in usdc.functions.approve.call_args_list] == [
            (ALLOWANCE_HOLDER_ADDRESS, 0), (ALLOWANCE_HOLDER_ADDRESS, BUY_IN)]

    @pytest.mark.parametrize("existing", [BUY_IN, BUY_IN * 3])
    def test_existing_allowance_at_or_above_amount_is_reused(self, executor, chain, signed, existing):
        usdc = chain.tokens[USDC.lower()]
        usdc.functions.allowance.return_value.call.return_value = existing
        _buy_quotes(executor)

        assert _buy(executor)["success"] is True
        usdc.functions.approve.assert_not_called()
        assert len(signed) == 1

    @pytest.mark.parametrize("allowance", [0, 5])
    def test_unconfirmed_approval_or_reset_refuses_without_quote(self, executor, chain, signed, allowance):
        chain.tokens[USDC.lower()].functions.allowance.return_value.call.return_value = allowance
        executor.trusted_w3.eth.wait_for_transaction_receipt.side_effect = [_receipt(status=0)]
        zerox = _buy_quotes(executor)

        result = _buy(executor)

        assert result == {"success": False, "error": "approval failed"}
        assert [kind for kind, _ in zerox.calls] == ["price"]
        assert len(signed) == 1

    def test_kill_switch_between_approval_and_quote(self, executor, chain, signed, tmp_path):
        chain.tokens[USDC.lower()].functions.allowance.return_value.call.return_value = 0

        def approval_mined(*args, **kwargs):
            (tmp_path / "kill").touch()
            return _receipt()
        executor.trusted_w3.eth.wait_for_transaction_receipt.side_effect = approval_mined
        _buy_quotes(executor)

        result = _buy(executor)

        assert result == {"success": False, "error": "kill switch engaged"}
        assert [tx["to"] for tx in signed] == [USDC]


class TestPostTradeCheck:
    def test_balance_shortfall_engages_kill_switch(self, executor, chain, caplog):
        chain.token(WETH, balances={BLOCK_NUMBER: 10**15, SWAP_BLOCK: 10**15 + floor(BUY_OUT) - 1})
        _buy_quotes(executor)

        with caplog.at_level(logging.CRITICAL):
            result = _buy(executor)

        assert result["confirmed"] is True
        assert os.path.exists(eth_mod.ONCHAIN_KILL_SWITCH_FILE)
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)
        assert executor.trusted_w3.eth.send_raw_transaction.call_count == 1
        blocks = [c.kwargs["block_identifier"] for c in chain.tokens[WETH.lower()].functions.balanceOf.return_value.call.call_args_list]
        assert blocks == [BLOCK_NUMBER, SWAP_BLOCK]

    def test_delta_exactly_at_floor_passes(self, executor, chain):
        chain.token(WETH, balances={BLOCK_NUMBER: 7, SWAP_BLOCK: 7 + floor(BUY_OUT)})
        _buy_quotes(executor)

        assert _buy(executor)["success"] is True
        assert not os.path.exists(eth_mod.ONCHAIN_KILL_SWITCH_FILE)

    def test_sell_shortfall_engages_kill_switch(self, executor, chain):
        chain.token(USDC, balances={BLOCK_NUMBER: 0, SWAP_BLOCK: floor(SELL_OUT) - 1})
        _sell_quotes(executor)

        assert _sell(executor)["confirmed"] is True
        assert os.path.exists(eth_mod.ONCHAIN_KILL_SWITCH_FILE)

    def test_next_swap_refused_after_shortfall(self, executor, chain):
        chain.token(WETH, balances={BLOCK_NUMBER: 0, SWAP_BLOCK: 1})
        _buy_quotes(executor)
        _buy(executor)

        assert _buy(executor) == {"success": False, "error": "kill switch engaged"}

    def test_reverted_swap_skips_fill_check(self, executor):
        _buy_quotes(executor)
        executor.trusted_w3.eth.wait_for_transaction_receipt.return_value = _receipt(status=0)
        executor._verify_fill = MagicMock()

        assert _buy(executor)["error"] == "transaction reverted"
        executor._verify_fill.assert_not_called()

    def test_unreadable_balance_logs_critical_without_kill_switch(self, executor, chain, caplog):
        chain.tokens[WETH.lower()].functions.balanceOf.return_value.call.side_effect = Exception("upstream 502")
        _buy_quotes(executor)

        with caplog.at_level(logging.CRITICAL):
            result = _buy(executor)

        assert result["confirmed"] is True
        assert any(r.levelno == logging.CRITICAL and "unverified" in r.getMessage() for r in caplog.records)
        assert not os.path.exists(eth_mod.ONCHAIN_KILL_SWITCH_FILE)

    def test_unwritable_kill_switch_file_never_raises(self, monkeypatch, tmp_path, caplog):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("")
        monkeypatch.setattr(eth_mod, "ONCHAIN_KILL_SWITCH_FILE", str(blocker / "kill"))

        with caplog.at_level(logging.CRITICAL):
            eth_mod.engage_onchain_kill_switch("test")

        assert any(r.levelno == logging.CRITICAL for r in caplog.records)

    def test_kill_switch_file_without_directory(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(eth_mod, "ONCHAIN_KILL_SWITCH_FILE", "kill")

        eth_mod.engage_onchain_kill_switch("test")

        assert (tmp_path / "kill").exists()


class TestExitFallback:
    def test_sell_falls_back_to_aerodrome_with_warning(self, executor, signed, caplog):
        executor.zerox = FakeZeroEx(price=ZeroExError("0x HTTP 503"), quote=ZeroExError("0x HTTP 503"))
        router = _install_aerodrome(executor, SELL_OUT)

        with caplog.at_level(logging.WARNING):
            result = _sell(executor)

        assert result["success"] is True
        assert any(r.levelno == logging.WARNING and "Aerodrome" in r.getMessage() for r in caplog.records)
        assert [tx["to"] for tx in signed] == [AERODROME_ROUTER]
        router.functions.swapExactTokensForTokens.assert_called_once()

    def test_fallback_sell_approves_the_aerodrome_router_for_exactly_the_sell_amount(self, executor, chain, signed):
        executor.zerox = FakeZeroEx(price=ZeroExError("0x HTTP 503"), quote=ZeroExError("0x HTTP 503"))
        weth = chain.tokens[WETH.lower()]
        weth.functions.allowance.return_value.call.return_value = 0
        _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor)["success"] is True

        weth.functions.approve.assert_called_once_with(AERODROME_ROUTER, SELL_IN)
        assert [tx["to"] for tx in signed] == [WETH, AERODROME_ROUTER]

    @pytest.mark.parametrize("case", sorted(SELL_REJECTIONS))
    def test_rejected_sell_quote_falls_back_and_never_signs_0x_data(self, executor, signed, case):
        changes, _ = SELL_REJECTIONS[case]
        _sell_quotes(executor, quote=with_changes(make_quote(WETH, USDC, SELL_IN, SELL_OUT), changes))
        _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor)["success"] is True
        assert [tx["to"] for tx in signed] == [AERODROME_ROUTER]

    def test_missing_client_sell_falls_back(self, executor, signed):
        executor.zerox = None
        _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor)["success"] is True
        assert [tx["to"] for tx in signed] == [AERODROME_ROUTER]

    def test_non_usdc_sell_never_falls_back(self, executor, signed):
        executor.zerox = FakeZeroEx(price=ZeroExError("down"), quote=ZeroExError("down"))
        router = _install_aerodrome(executor, 10**18)

        result = executor.execute_swap(WETH, AERO, SELL_IN, executor.account.address, reference_price=2500)

        assert result["success"] is False
        router.functions.getAmountsOut.assert_not_called()
        _assert_nothing_signed(executor, signed)

    @pytest.mark.parametrize("reference", [None, 0])
    def test_fallback_never_runs_without_reference(self, executor, signed, reference):
        executor.zerox = FakeZeroEx(price=ZeroExError("down"), quote=ZeroExError("down"))
        router = _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor, reference_price=reference)["success"] is False
        router.functions.getAmountsOut.assert_not_called()
        _assert_nothing_signed(executor, signed)

    def test_preflight_refusal_does_not_fall_back(self, executor, signed):
        executor.trusted_w3.eth.get_balance.return_value = 0
        router = _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor) == {"success": False, "error": "insufficient gas"}
        router.functions.getAmountsOut.assert_not_called()
        _assert_nothing_signed(executor, signed)

    def test_fallback_into_empty_pool_refused(self, executor, signed):
        executor.zerox = FakeZeroEx(price=ZeroExError("down"), quote=ZeroExError("down"))
        router = _install_aerodrome(executor, 0)

        result = _sell(executor)

        assert result["success"] is False and "quote" in result["error"]
        router.functions.swapExactTokensForTokens.assert_not_called()
        _assert_nothing_signed(executor, signed)

    def test_fallback_into_thin_pool_refused_by_reference_guard(self, executor, signed):
        executor.zerox = FakeZeroEx(price=ZeroExError("down"), quote=ZeroExError("down"))
        router = _install_aerodrome(executor, 1500 * 10**6)

        result = _sell(executor)

        assert result == {"success": False, "error": "quote deviates from reference price"}
        router.functions.swapExactTokensForTokens.assert_not_called()
        _assert_nothing_signed(executor, signed)

    def test_no_fallback_once_the_0x_swap_was_broadcast(self, executor, signed):
        _sell_quotes(executor)
        executor.trusted_w3.eth.wait_for_transaction_receipt.return_value = _receipt(status=0)
        router = _install_aerodrome(executor, SELL_OUT)

        result = _sell(executor)

        assert result["success"] is False and result["error"] == "transaction reverted"
        router.functions.getAmountsOut.assert_not_called()
        assert len(signed) == 1

    def test_no_fallback_on_kill_switch(self, executor, tmp_path):
        (tmp_path / "kill").touch()
        router = _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor) == {"success": False, "error": "kill switch engaged"}
        router.functions.getAmountsOut.assert_not_called()


class TestStrandedTokens:
    def _executor(self, balance):
        ex = EthereumExecutor("http://read.invalid", TEST_KEY, "live", trusted_rpc_url="http://trusted.invalid")
        ex.w3 = MagicMock()
        call = ex.w3.eth.contract.return_value.functions.balanceOf.return_value.call
        if isinstance(balance, Exception):
            call.side_effect = balance
        else:
            call.return_value = balance
        return ex

    def test_held_wbtc_logs_loud_warning_and_is_not_sold(self, caplog):
        ex = self._executor(5 * 10**6)

        with caplog.at_level(logging.WARNING):
            ex.warn_about_stranded_tokens()

        assert any(r.levelno == logging.WARNING and "WBTC" in r.getMessage() and WBTC in r.getMessage()
                   for r in caplog.records)
        assert ex.w3.eth.contract.call_args.kwargs["address"] == WBTC

    @pytest.mark.parametrize("balance", [0, eth_mod.LEGACY_TOKENS["WBTC"][1]])
    def test_dust_or_empty_is_quiet(self, caplog, balance):
        with caplog.at_level(logging.WARNING):
            self._executor(balance).warn_about_stranded_tokens()

        assert not any("WBTC" in r.getMessage() for r in caplog.records)

    def test_read_failure_never_raises(self):
        self._executor(Exception("rpc down")).warn_about_stranded_tokens()


class TestRouterSelection:
    def test_default_router_is_aerodrome_and_builds_no_client_even_with_key(self, monkeypatch):
        monkeypatch.setenv("ZEROX_API_KEY", "k")

        ex = EthereumExecutor("http://read.invalid", TEST_KEY, "live", trusted_rpc_url="http://trusted.invalid")

        assert ex.onchain_router == "aerodrome"
        assert ex.zerox is None

    def test_aerodrome_mode_never_touches_0x(self, executor, signed):
        executor.onchain_router = "aerodrome"
        executor.zerox = FakeZeroEx(price=AssertionError("0x used"), quote=AssertionError("0x used"))
        _install_aerodrome(executor, SELL_OUT)

        assert _sell(executor)["success"] is True
        executor.router_contract.functions.getAmountsOut.side_effect = None
        executor.router_contract.functions.getAmountsOut.return_value.call.return_value = [BUY_IN, BUY_OUT]
        assert _buy(executor)["success"] is True
        assert executor.zerox.calls == []
        assert {tx["to"] for tx in signed} == {AERODROME_ROUTER}

    def test_unknown_router_rejected(self, monkeypatch):
        monkeypatch.setattr(eth_mod, "ONCHAIN_ROUTER", "uniswap")

        with pytest.raises(ValueError):
            EthereumExecutor("http://read.invalid", TEST_KEY, "live", trusted_rpc_url="http://trusted.invalid")

    def test_zerox_router_builds_client_from_env_key(self, monkeypatch):
        monkeypatch.setattr(eth_mod, "ONCHAIN_ROUTER", "zerox")
        monkeypatch.setenv("ZEROX_API_KEY", "k")

        ex = EthereumExecutor("http://read.invalid", TEST_KEY, "live", trusted_rpc_url="http://trusted.invalid")

        assert ex.zerox is not None

    def test_round_trip_fee_default_and_bad_env_fails_loudly_at_import(self):
        assert eth_mod.ONCHAIN_ROUND_TRIP_FEE_PCT == Decimal("0.006")
        env = {**os.environ, "ONCHAIN_ROUND_TRIP_FEE_PCT": "not-a-number"}

        proc = subprocess.run([sys.executable, "-c", "import executors.ethereum_executor"],
                              cwd=REPO, env=env, capture_output=True, text=True)

        assert proc.returncode != 0 and "InvalidOperation" in proc.stderr


NEW_TOKENS = {
    "VVV": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
    "AAVE": "0x63706e401c06ac8513145b7687A14804d17f814b",
    "UNI": "0xc3De830EA07524a0761646a6a4e4be0e114a3C83",
}


class TestRegistryAdditions:
    @pytest.mark.parametrize("symbol, address", NEW_TOKENS.items())
    def test_registered_with_18_decimals(self, symbol, address):
        assert TOKENS[symbol] == address
        assert Web3.is_checksum_address(address)
        assert eth_mod.KNOWN_DECIMALS[address] == 18

    def test_exposed_as_supported_assets(self, executor):
        assert set(NEW_TOKENS) <= set(executor.get_supported_assets())

    def test_decimals_need_no_rpc(self, executor):
        executor.trusted_w3.eth.contract.reset_mock()

        assert [executor._get_decimals(a) for a in NEW_TOKENS.values()] == [18, 18, 18]
        executor.trusted_w3.eth.contract.assert_not_called()

    @pytest.mark.parametrize("symbol, address", NEW_TOKENS.items())
    def test_pass_the_zerox_allowlist_in_both_directions(self, executor, chain, signed, symbol, address):
        chain.token(address)
        amount_out = 4 * 10**18
        _quotes(executor, USDC, address, BUY_IN, amount_out)

        assert executor.execute_swap(USDC, address, BUY_IN, executor.account.address, reference_price=25)["success"] is True

        _quotes(executor, address, USDC, SELL_IN, 25 * 10**6)
        assert executor.execute_swap(address, USDC, SELL_IN, executor.account.address, reference_price=25)["success"] is True

    def test_unknown_token_is_still_refused(self, executor, signed):
        unknown = "0x2222222222222222222222222222222222222222"

        result = executor.execute_swap(USDC, unknown, BUY_IN, executor.account.address, reference_price=25)

        assert result["success"] is False and "allowlist" in result["error"]
        assert executor.zerox.calls == []
        _assert_nothing_signed(executor, signed)


REAL_QUOTE_PATH = os.path.join(REPO, "tests", "fixtures", "zerox_real_quote_usdc_weth.json")
REAL_SELL_AMOUNT = 25 * 10**6
REAL_REFERENCE_PRICE = 2240  # USDC per WETH implied by the capture (25 / 0.01116)


@pytest.fixture
def real_quote():
    with open(REAL_QUOTE_PATH) as f:
        return json.loads(base64.b64decode(json.load(f)["body_b64"]))


@pytest.fixture
def real_taker():
    with open(REAL_QUOTE_PATH) as f:
        return json.load(f)["request_params"]["taker"]


@pytest.fixture
def real_executor(executor, real_taker):
    executor.account = SimpleNamespace(address=real_taker)
    return executor


def retampered(data, **changes):
    """Re-encode AllowanceHolder.exec calldata with some of its (and the inner Settler call's) fields replaced."""
    operator, token, amount, target, settler_call = decode(
        ["address", "address", "uint256", "address", "bytes"], bytes.fromhex(data[10:]))
    (recipient, buy_token, min_out), actions, zid = decode(
        ["(address,address,uint256)", "bytes[]", "bytes32"], settler_call[4:])
    fields = {"operator": operator, "token": token, "amount": amount, "target": target,
              "recipient": recipient, "buy_token": buy_token, "min_out": min_out, **changes}
    inner = bytes.fromhex(EXECUTE_SELECTOR[2:]) + encode(
        ["(address,address,uint256)", "bytes[]", "bytes32"],
        [(fields["recipient"], fields["buy_token"], fields["min_out"]), actions, zid])
    return EXEC_SELECTOR + encode(
        ["address", "address", "uint256", "address", "bytes"],
        [fields["operator"], fields["token"], fields["amount"], fields["target"], inner]).hex()


class TestRealCapturedQuote:
    """A live signed Base /quote (USDC -> WETH, taker = captured wallet) through the real validators."""

    def _check_tx(self, executor, quote, our_min=None):
        our_min = our_min if our_min is not None else executor._amount_out_minimum(int(quote["buyAmount"]))
        return executor._check_zerox_transaction(quote, USDC, WETH, REAL_SELL_AMOUNT, our_min)

    def test_transaction_accepted_through_the_dwell_branch(self, real_executor, real_quote, chain):
        assert real_quote["transaction"]["data"].lower().count(PREV_SETTLER[2:].lower()) >= 2

        call = self._check_tx(real_executor, real_quote)

        assert call.to == ALLOWANCE_HOLDER_ADDRESS
        assert call.data == real_quote["transaction"]["data"]
        chain.registry.functions.prev.assert_called_once_with(2)

    def test_the_capture_sits_exactly_on_our_slippage_floor(self, real_executor, real_quote):
        assert real_executor._amount_out_minimum(int(real_quote["buyAmount"])) == int(real_quote["minBuyAmount"])

    @pytest.mark.parametrize("changes, reason", [
        ({"transaction__to": STRANGER}, "not AllowanceHolder"),
        ({"transaction__value": "1"}, "must be 0"),
    ])
    def test_tampered_envelope_rejected(self, real_executor, real_quote, changes, reason):
        with pytest.raises(eth_mod.ZeroExQuoteRejected, match=reason):
            self._check_tx(real_executor, with_changes(real_quote, changes))

    @pytest.mark.parametrize("change, reason", [
        ({"recipient": STRANGER}, "recipient"),
        ({"buy_token": USDC}, "buyToken"),
        ({"min_out": 11104989294834023 - 1}, "below our floor"),
        ({"token": WETH}, "token/amount"),
        ({"amount": REAL_SELL_AMOUNT + 1}, "token/amount"),
        ({"target": STRANGER, "operator": STRANGER}, "neither Settler"),
    ])
    def test_tampered_calldata_rejected(self, real_executor, real_quote, change, reason):
        data = retampered(real_quote["transaction"]["data"], **change)

        with pytest.raises(eth_mod.ZeroExQuoteRejected, match=reason):
            self._check_tx(real_executor, with_changes(real_quote, {"transaction__data": data}))

    def test_retampering_helper_is_lossless(self, real_quote):
        data = real_quote["transaction"]["data"]

        assert retampered(data).lower() == data.lower()

    def test_response_rejected_while_the_wallet_cannot_cover_the_sell(self, real_executor, real_quote):
        assert real_quote["issues"]["balance"] is not None

        with pytest.raises(eth_mod.ZeroExQuoteRejected, match="insufficient sell-token balance"):
            real_executor._check_zerox_response(
                real_quote, USDC, WETH, REAL_SELL_AMOUNT, REAL_REFERENCE_PRICE, firm=True)

    def test_response_accepted_once_the_balance_issue_is_cleared(self, real_executor, real_quote):
        real_quote["issues"]["balance"] = None

        our_min = real_executor._check_zerox_response(
            real_quote, USDC, WETH, REAL_SELL_AMOUNT, REAL_REFERENCE_PRICE, firm=True)

        assert our_min == int(real_quote["minBuyAmount"])

    def test_cleared_response_still_rejects_a_mismatched_order(self, real_executor, real_quote):
        real_quote["issues"]["balance"] = None

        with pytest.raises(eth_mod.ZeroExQuoteRejected, match="does not match"):
            real_executor._check_zerox_response(
                real_quote, USDC, WETH, REAL_SELL_AMOUNT + 1, REAL_REFERENCE_PRICE, firm=True)
