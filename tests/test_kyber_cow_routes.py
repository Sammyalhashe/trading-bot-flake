"""EthereumExecutor KyberSwap and CoW routes, and wrap_eth. All HTTP and trusted-RPC calls are mocked."""
import copy
import os
import sys
from unittest.mock import MagicMock

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from web3 import Web3
from web3.datastructures import AttributeDict

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]

import executors.ethereum_executor as eth_mod  # noqa: E402
from executors.ethereum_executor import (  # noqa: E402
    COW_SETTLEMENT_ADDRESS,
    COW_VAULT_RELAYER_ADDRESS,
    KYBER_ROUTER_ADDRESS,
)
from executors.kyberswap_client import KyberSwapError  # noqa: E402
from test_zerox_router import (  # noqa: E402,F401
    BLOCK_TS,
    STRANGER,
    USDC,
    WALLET,
    WETH,
    chain,
    executor,
    floor,
    nonces,
    signed,
)

BTC = eth_mod.TOKENS["BTC"]
KYBER_EXECUTOR = "0x8f10b468b06c6fd214b65f87778827f7d113f996"  # callTarget in a live Base build
BUY_IN, BUY_OUT = 25 * 10**6, 39_245          # 25 USDC -> 0.00039245 cbBTC (~$63,700)
REFERENCE = 63_700
KYBER_SWAP_TYPES = ["(address,address,bytes,(address,address,address[],uint256[],address[],uint256[],"
                    "address,uint256,uint256,uint256,bytes),bytes)"]
ONCHAIN_COW_DOMAIN_SEPARATOR = "d72ffa789b6fae41254d0b5a13e6e1e92ed947ec6a251edf1cf0b6c02c257b4b"
GPV2_ORDER_TYPEHASH = "d5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489"


def kyber_calldata(src=USDC, dst=BTC, amount=BUY_IN, receiver=None, min_return=None, fee_receivers=(),
                   fee_amounts=(), permit=b"", selector="0xe21fd0e9", src_amounts=None):
    desc = (src, dst, [KYBER_EXECUTOR], list(src_amounts or [amount]), list(fee_receivers), list(fee_amounts),
            receiver or WALLET, amount, floor(BUY_OUT) if min_return is None else min_return, 512, permit)
    return selector + encode(KYBER_SWAP_TYPES, [(KYBER_EXECUTOR, "0x" + "00" * 20, b"\x01", desc, b"")]).hex()


def kyber_build(**changes):
    built = {"amountIn": str(BUY_IN), "amountOut": str(BUY_OUT), "gas": "666745",
             "routerAddress": KYBER_ROUTER_ADDRESS, "transactionValue": "0", "data": kyber_calldata()}
    built.update(changes)
    return built


class FakeKyber:
    def __init__(self, amount_out=BUY_OUT, built=None):
        self.amount_out = amount_out
        self.built = built or kyber_build()
        self.calls = []

    def routes(self, token_in, token_out, amount_in):
        self.calls.append(("routes", token_in, token_out, amount_in))
        return {"amountIn": str(amount_in), "amountOut": str(self.amount_out), "tokenIn": token_in.lower()}

    def build(self, route_summary, sender, recipient, slippage_bps):
        self.calls.append(("build", sender, recipient, slippage_bps))
        if isinstance(self.built, BaseException):
            raise self.built
        return copy.deepcopy(self.built)


def cow_quote(**changes):
    quote = {"sellToken": USDC.lower(), "buyToken": BTC.lower(), "receiver": WALLET.lower(),
             "sellAmount": str(BUY_IN - 3_967), "feeAmount": "3967", "buyAmount": str(BUY_OUT),
             "validTo": BLOCK_TS + 1800, "appData": "{}", "kind": "sell", "partiallyFillable": False,
             "sellTokenBalance": "erc20", "buyTokenBalance": "erc20", "signingScheme": "eip712"}
    quote.update(changes)
    return {"quote": quote, "from": WALLET.lower(), "id": 710419104, "protocolFeeBps": "2"}


class FakeCow:
    def __init__(self, quote=None, statuses=("open", "fulfilled")):
        self.quote_body = quote or cow_quote()
        self.statuses = list(statuses)
        self.posted = []
        self.calls = []

    def quote(self, body):
        self.calls.append(("quote", body))
        return copy.deepcopy(self.quote_body)

    def post_order(self, body):
        self.posted.append(body)
        return "0x" + "ab" * 56

    def order(self, uid):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"uid": uid, "status": status, "executedBuyAmount": str(BUY_OUT)}

    def trades(self, uid):
        return [{"txHash": "0x" + "cd" * 32, "blockNumber": 38_000_001}]


@pytest.fixture
def kyber(executor, chain):
    chain.token(BTC)
    chain.token(USDC, allowance=0)
    executor.kyber = FakeKyber()
    return executor.kyber


@pytest.fixture
def cow(executor, chain, monkeypatch):
    chain.token(USDC, allowance=0)
    balances = iter([0, BUY_OUT])
    chain.token(BTC).functions.balanceOf.return_value.call.side_effect = lambda block_identifier: next(balances)
    monkeypatch.setattr(eth_mod.time, "sleep", lambda s: None)
    settle(executor, [transfer_log(BTC, COW_SETTLEMENT_ADDRESS, WALLET, BUY_OUT)])
    executor.cow = FakeCow()
    return executor.cow


TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)")


def transfer_log(token, sender, to, amount):
    pad = lambda a: bytes(12) + bytes.fromhex(a[2:])
    return AttributeDict({"address": Web3.to_checksum_address(token),
                          "topics": [TRANSFER_TOPIC, pad(sender), pad(to)], "data": amount.to_bytes(32, "big")})


def settle(executor, logs, status=1):
    receipt = AttributeDict({"status": status, "gasUsed": 400_000, "blockNumber": 38_000_001, "logs": logs})
    executor.trusted_w3.eth.get_transaction_receipt.side_effect = (
        lambda h: receipt if h == "0x" + "cd" * 32 else AttributeDict({"status": 1, "blockNumber": 38_000_001}))


def buy(executor, router, reference_price=REFERENCE, recipient=None):
    return executor.execute_swap(USDC, BTC, BUY_IN, recipient or WALLET, reference_price=reference_price, router=router)


def approvals(chain, token):
    return [c.args for c in chain.tokens[token.lower()].functions.approve.call_args_list]


class TestKyberHappyPath:
    def test_signs_validated_kyber_calldata(self, executor, chain, kyber, signed):
        result = buy(executor, "kyberswap")

        assert result["success"] is True and result["confirmed"] is True
        tx = signed[-1]
        assert tx["to"] == KYBER_ROUTER_ADDRESS and tx["value"] == 0
        assert tx["data"] == kyber.built["data"]
        assert approvals(chain, USDC) == [(KYBER_ROUTER_ADDRESS, BUY_IN)]
        assert ("build", WALLET, WALLET, eth_mod.SLIPPAGE_BPS) in kyber.calls
        assert result["quoted_out"] == BUY_OUT and result["min_out"] == floor(BUY_OUT)
        assert result["received"] == 10**30

    def test_live_bot_router_unchanged_without_override(self, executor, kyber, signed):
        executor.onchain_router = "aerodrome"
        executor._execute_aerodrome_swap = MagicMock(return_value={"success": False, "error": "aero"})
        assert executor.execute_swap(USDC, BTC, BUY_IN, WALLET, reference_price=REFERENCE)["error"] == "aero"
        assert kyber.calls == []


KYBER_REJECTIONS = {
    "wrong_router": ({"routerAddress": STRANGER}, "routerAddress"),
    "nonzero_value": ({"transactionValue": "1"}, "transactionValue"),
    "foreign_receiver": ({"data": kyber_calldata(receiver=STRANGER)}, "dstReceiver"),
    "min_below_floor": ({"data": kyber_calldata(min_return=floor(BUY_OUT) - 1)}, "minReturnAmount"),
    "wrong_src": ({"data": kyber_calldata(src=WETH)}, "does not match"),
    "wrong_dst": ({"data": kyber_calldata(dst=WETH)}, "does not match"),
    "wrong_amount": ({"data": kyber_calldata(amount=BUY_IN + 1)}, "does not match"),
    "src_amounts_exceed": ({"data": kyber_calldata(src_amounts=[BUY_IN, 1])}, "srcAmounts"),
    "fee_skim": ({"data": kyber_calldata(fee_receivers=[STRANGER], fee_amounts=[10])}, "fee"),
    "permit": ({"data": kyber_calldata(permit=b"\x01")}, "permit"),
    "other_selector": ({"data": kyber_calldata(selector="0x8af033fb")}, "not MetaAggregationRouterV2.swap"),
    "undecodable": ({"data": "0xe21fd0e9" + "01" * 8}, "malformed"),
}


class TestKyberRejections:
    @pytest.mark.parametrize("name", sorted(KYBER_REJECTIONS))
    def test_refused_before_signing(self, executor, kyber, signed, name):
        changes, reason = KYBER_REJECTIONS[name]
        kyber.built = kyber_build(**changes)
        approvals_before = len([t for t in signed if t["data"] == "0x095ea7b3"])

        result = buy(executor, "kyberswap")

        assert result["success"] is False and reason in result["error"]
        assert all(t["data"] == "0x095ea7b3" for t in signed)
        assert len(signed) >= approvals_before

    def test_quote_far_from_reference_refused_before_approval(self, executor, kyber, signed):
        kyber.amount_out = BUY_OUT // 2
        assert buy(executor, "kyberswap")["success"] is False
        assert signed == []

    def test_requires_reference_price_and_own_wallet(self, executor, kyber, signed):
        assert buy(executor, "kyberswap", reference_price=None)["success"] is False
        assert buy(executor, "kyberswap", recipient=STRANGER)["success"] is False
        assert signed == []

    def test_api_error_refuses(self, executor, kyber, signed):
        kyber.built = KyberSwapError("route expired")
        result = buy(executor, "kyberswap")
        assert result["success"] is False and "route expired" in result["error"]
        assert all(t["data"] == "0x095ea7b3" for t in signed)

    def test_token_outside_registry_refused(self, executor, kyber, signed):
        result = executor.execute_swap(USDC, STRANGER, BUY_IN, WALLET, reference_price=1, router="kyberswap")
        assert result["success"] is False and signed == []

    def test_router_without_code_refused_before_approval(self, executor, kyber, signed):
        executor.trusted_w3.eth.get_code.return_value = b""
        assert buy(executor, "kyberswap")["success"] is False
        assert signed == []

    def test_kill_switch_blocks(self, executor, kyber, signed, tmp_path):
        (tmp_path / "kill").write_text("x")
        assert buy(executor, "kyberswap")["success"] is False
        assert signed == []


class TestCowSigning:
    def test_typehash_and_domain_match_gpv2(self):
        assert eth_mod.COW_ORDER_TYPEHASH.hex() == GPV2_ORDER_TYPEHASH
        assert eth_mod.COW_DOMAIN_SEPARATOR.hex() == ONCHAIN_COW_DOMAIN_SEPARATOR

    def test_signature_recovers_wallet(self, executor):
        order = executor._cow_order(USDC, BTC, BUY_IN, floor(BUY_OUT), valid_to=BLOCK_TS + 600)
        signature = executor._sign_cow_order(order)
        digest = eth_mod.cow_order_digest(order)
        assert Account._recover_hash(digest, signature=bytes.fromhex(signature[2:])) == WALLET

    def test_digest_matches_manual_eip712(self, executor):
        order = executor._cow_order(USDC, BTC, BUY_IN, 123, valid_to=1_900_000_000)
        struct = keccak(encode(
            ["bytes32", "address", "address", "address", "uint256", "uint256", "uint32", "bytes32", "uint256",
             "bytes32", "bool", "bytes32", "bytes32"],
            [bytes.fromhex(GPV2_ORDER_TYPEHASH), USDC, BTC, WALLET, BUY_IN, 123, 1_900_000_000,
             keccak(text="{}"), 0, keccak(text="sell"), False, keccak(text="erc20"), keccak(text="erc20")]))
        expected = keccak(b"\x19\x01" + bytes.fromhex(ONCHAIN_COW_DOMAIN_SEPARATOR) + struct)
        assert eth_mod.cow_order_digest(order) == expected


class TestCowHappyPath:
    def test_posts_our_own_order_and_waits_for_fill(self, executor, chain, cow, signed):
        result = buy(executor, "cow")

        assert result["success"] is True and result["confirmed"] is True
        assert result["received"] == BUY_OUT and result["quoted_out"] == BUY_OUT
        assert result["tx_hash"] == "0x" + "cd" * 32 and result["order_uid"].startswith("0xabab")
        body = cow.posted[0]
        assert body["receiver"].lower() == WALLET.lower() and body["from"].lower() == WALLET.lower()
        assert int(body["sellAmount"]) == BUY_IN and body["feeAmount"] == "0"
        assert int(body["buyAmount"]) == floor(BUY_OUT)
        assert body["kind"] == "sell" and body["partiallyFillable"] is False
        assert body["appData"] == "{}" and body["signingScheme"] == "eip712" and body["quoteId"] == 710419104
        assert BLOCK_TS < body["validTo"] <= BLOCK_TS + eth_mod.COW_ORDER_VALIDITY_SECONDS
        assert approvals(chain, USDC) == [(COW_VAULT_RELAYER_ADDRESS, BUY_IN)]
        assert all(t["data"] == "0x095ea7b3" for t in signed)

    def test_expired_order_resets_allowance(self, executor, chain, cow):
        cow.statuses = ["open", "expired"]
        result = buy(executor, "cow")
        assert result["success"] is False and "expired" in result["error"]
        assert approvals(chain, USDC)[-1] == (COW_VAULT_RELAYER_ADDRESS, 0)

    def test_short_fill_engages_kill_switch(self, executor, chain, cow, tmp_path):
        settle(executor, [transfer_log(BTC, COW_SETTLEMENT_ADDRESS, WALLET, floor(BUY_OUT) - 1)])
        buy(executor, "cow")
        assert (tmp_path / "kill").exists()

    def test_other_inflow_cannot_hide_short_fill(self, executor, chain, cow, tmp_path):
        balances = iter([0, BUY_OUT * 3])
        chain.tokens[BTC.lower()].functions.balanceOf.return_value.call.side_effect = lambda block_identifier: next(balances)
        settle(executor, [transfer_log(BTC, COW_SETTLEMENT_ADDRESS, WALLET, floor(BUY_OUT) - 1)])
        result = buy(executor, "cow")
        assert result["received"] == floor(BUY_OUT) - 1
        assert (tmp_path / "kill").exists()

    def test_only_settlement_transfers_of_bought_token_to_us_count(self, executor, cow):
        settle(executor, [transfer_log(BTC, COW_SETTLEMENT_ADDRESS, WALLET, BUY_OUT),
                          transfer_log(BTC, STRANGER, WALLET, 999),
                          transfer_log(BTC, COW_SETTLEMENT_ADDRESS, STRANGER, 999),
                          transfer_log(WETH, COW_SETTLEMENT_ADDRESS, WALLET, 999)])
        assert buy(executor, "cow")["received"] == BUY_OUT

    def test_reverted_settlement_counts_nothing(self, executor, cow, tmp_path):
        settle(executor, [transfer_log(BTC, COW_SETTLEMENT_ADDRESS, WALLET, BUY_OUT)], status=0)
        assert buy(executor, "cow")["received"] == 0
        assert (tmp_path / "kill").exists()

    def test_falls_back_to_balance_without_settlement_tx(self, executor, cow):
        cow.trades = lambda uid: []
        assert buy(executor, "cow")["received"] == BUY_OUT


COW_REJECTIONS = {
    "wrong_sell_token": {"sellToken": WETH.lower()},
    "wrong_buy_token": {"buyToken": WETH.lower()},
    "amounts_dont_add_up": {"sellAmount": str(BUY_IN)},
    "buy_kind": {"kind": "buy"},
    "foreign_receiver": {"receiver": STRANGER.lower()},
    "partially_fillable": {"partiallyFillable": True},
    "far_from_reference": {"buyAmount": str(BUY_OUT // 2)},
}


class TestCowRejections:
    @pytest.mark.parametrize("name", sorted(COW_REJECTIONS))
    def test_refused_before_signing(self, executor, cow, signed, name):
        cow.quote_body = cow_quote(**COW_REJECTIONS[name])
        assert buy(executor, "cow")["success"] is False
        assert cow.posted == [] and signed == []

    def test_kill_switch_blocks_order_signature(self, executor, cow, tmp_path, monkeypatch):
        real_approve = executor._approve_token
        monkeypatch.setattr(executor, "_approve_token",
                            lambda *a, **k: ((tmp_path / "kill").write_text("x"), real_approve(*a, **k))[1])
        assert buy(executor, "cow")["success"] is False
        assert cow.posted == []


class TestKillSwitchIsolated:
    """Each check must hold on its own, not only via _send_contract_tx's pre-signature check."""

    def test_cow_switch_engaged_after_approval_blocks_signing(self, executor, cow, tmp_path, monkeypatch):
        real_approve = executor._approve_token

        def approve_then_engage(*args, **kwargs):
            result = real_approve(*args, **kwargs)
            (tmp_path / "kill").write_text("x")
            return result
        monkeypatch.setattr(executor, "_approve_token", approve_then_engage)
        sign = MagicMock(wraps=executor._sign_cow_order)
        monkeypatch.setattr(executor, "_sign_cow_order", sign)

        assert buy(executor, "cow")["success"] is False
        sign.assert_not_called()
        assert cow.posted == []

    def test_wrap_eth_checks_switch_before_anything_else(self, executor, tmp_path, monkeypatch):
        send = MagicMock()
        monkeypatch.setattr(executor, "_send_contract_tx", send)
        (tmp_path / "kill").write_text("x")
        assert executor.wrap_eth(10**16)["success"] is False
        send.assert_not_called()


class TestWrapEth:
    @pytest.fixture(autouse=True)
    def weth_deposit(self, chain):
        deposit = chain.tokens[WETH.lower()].functions.deposit.return_value
        deposit.estimate_gas.return_value = 30_000
        deposit.build_transaction.side_effect = lambda params: {
            **params, "to": Web3.to_checksum_address(WETH), "data": "0xd0e30db0"}

    def test_wraps_with_value_to_weth_only(self, executor, signed):
        result = executor.wrap_eth(2 * 10**16)
        assert result["success"] is True
        tx = signed[-1]
        assert tx["to"] == Web3.to_checksum_address(WETH) and tx["value"] == 2 * 10**16

    def test_refuses_to_dip_below_gas_reserve(self, executor, signed):
        executor.trusted_w3.eth.get_balance.return_value = 2 * 10**16
        assert executor.wrap_eth(2 * 10**16)["success"] is False
        assert signed == []

    def test_kill_switch_blocks(self, executor, signed, tmp_path):
        (tmp_path / "kill").write_text("x")
        assert executor.wrap_eth(10**16)["success"] is False
        assert signed == []

    def test_swaps_still_send_zero_value(self, executor, kyber, signed):
        buy(executor, "kyberswap")
        assert all(t["value"] == 0 for t in signed)
