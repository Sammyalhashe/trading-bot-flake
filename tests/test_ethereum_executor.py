import pytest
from unittest.mock import MagicMock
import sys
import os
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import ContractLogicError, TimeExhausted, TransactionNotFound

import executors.ethereum_executor as eth_mod
from executors.ethereum_executor import (
    AERODROME_POOL_FACTORY_ADDRESS,
    AERODROME_ROUTER_ADDRESS,
    TOKENS,
    MAX_PRIORITY_FEE_WEI,
    TX_DEADLINE_SECONDS,
    EthereumExecutor,
)

TEST_KEY = "0x" + "11" * 32
USDC = TOKENS["USDC"]
WETH = TOKENS["WETH"]
BLOCK_TS = 1_800_000_000
BASE_FEE = 5_000_000
PRIORITY_FEE = 1_000_000
GAS_ESTIMATE = 200_000


def _install_router(executor, quote_for_routes):
    """Mock Aerodrome Router. quote_for_routes(routes) returns the final amount or an Exception."""
    router = MagicMock()

    def get_amounts_out(amount_in, routes):
        call = MagicMock()
        out = quote_for_routes(routes)
        if isinstance(out, Exception):
            call.call.side_effect = out
        else:
            call.call.return_value = [amount_in] + [out] * len(routes)
        return call

    router.functions.getAmountsOut.side_effect = get_amounts_out
    swap_fn = MagicMock()
    swap_fn.estimate_gas.return_value = GAS_ESTIMATE
    swap_fn.build_transaction.side_effect = lambda params: {
        **params, "to": Web3.to_checksum_address(AERODROME_ROUTER_ADDRESS), "data": "0xcac88ea9",
    }
    router.functions.swapExactTokensForTokens.return_value = swap_fn
    executor.router_contract = router
    return router, swap_fn


def _install_token(executor, allowance):
    """Mock ERC20 behind the trusted RPC; the allowance read is what _approve_token acts on."""
    token = MagicMock()
    token.functions.allowance.return_value.call.return_value = allowance
    approve_fn = token.functions.approve.return_value
    approve_fn.estimate_gas.return_value = 50_000
    approve_fn.build_transaction.side_effect = lambda params: {**params, "to": Web3.to_checksum_address(USDC), "data": "0x095ea7b3"}
    executor.trusted_w3.eth.contract.return_value = token
    return token


@pytest.fixture
def nonces():
    return {"pending": 7, "latest": 7}


@pytest.fixture
def executor(monkeypatch, tmp_path, nonces):
    """Live-mode executor with every trusted-RPC call mocked; no network access."""
    monkeypatch.delenv("ONCHAIN_KILL_SWITCH", raising=False)
    monkeypatch.setattr(eth_mod, "ONCHAIN_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    ex = EthereumExecutor("http://read.invalid", TEST_KEY, "live", trusted_rpc_url="http://trusted.invalid")

    ex.trusted_w3 = MagicMock()
    eth = ex.trusted_w3.eth
    eth.chain_id = 8453
    eth.get_block.return_value = {"timestamp": BLOCK_TS, "baseFeePerGas": BASE_FEE}
    eth.max_priority_fee = PRIORITY_FEE
    eth.get_balance.return_value = 10**18
    eth.get_transaction_count.side_effect = lambda addr, block: nonces[block]
    eth.send_raw_transaction.side_effect = lambda raw: Web3.keccak(raw)
    eth.wait_for_transaction_receipt.return_value = AttributeDict({"status": 1, "gasUsed": 150_000})

    _install_token(ex, allowance=2**256 - 1)
    return ex


def _best_direct_volatile(routes):
    if len(routes) == 1 and routes[0][2] is False:
        return 1_000_000
    return 900_000


class TestQuoteRequired:
    def test_quote_failure_rejects_swap(self, executor):
        router, _ = _install_router(executor, lambda routes: ContractLogicError("execution reverted"))

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is False
        assert "quote" in result["error"]
        router.functions.swapExactTokensForTokens.assert_not_called()
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()

    def test_zero_quote_from_missing_pools_rejects_swap(self, executor):
        router, _ = _install_router(executor, lambda routes: 0)

        result = executor.execute_swap(USDC, WETH, 10 * 10**6, executor.account.address)

        assert result["success"] is False
        router.functions.swapExactTokensForTokens.assert_not_called()
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()

    def test_candidate_routes_use_verified_struct_order(self, executor):
        routes = executor._candidate_routes(USDC, TOKENS["AERO"])
        factory = Web3.to_checksum_address(AERODROME_POOL_FACTORY_ADDRESS)

        assert [(r[0][2], len(r)) for r in routes[:2]] == [(False, 1), (True, 1)]
        assert all(hop[3] == factory and isinstance(hop[2], bool) for r in routes for hop in r)
        assert len(executor._candidate_routes(USDC, WETH)) == 2


class TestSlippageMinimum:
    def test_amount_out_min_from_best_quote(self, executor):
        router, _ = _install_router(executor, _best_direct_volatile)

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result == {"success": True, "confirmed": True, "tx_hash": result["tx_hash"]}
        amount_in, amount_out_min, routes, recipient, _ = router.functions.swapExactTokensForTokens.call_args[0]
        assert amount_in == 10**17
        assert amount_out_min == 995_000
        assert routes == [(Web3.to_checksum_address(WETH), Web3.to_checksum_address(USDC), False,
                           Web3.to_checksum_address(AERODROME_POOL_FACTORY_ADDRESS))]
        assert recipient == executor.account.address

    def test_amount_out_minimum_math_and_bounds(self, executor):
        assert executor._amount_out_minimum(1_000_000, slippage_bps=100) == 990_000
        assert executor._amount_out_minimum(1_000_000) == 995_000
        for bad in (0, -5, 301):
            with pytest.raises(ValueError):
                executor._amount_out_minimum(1_000_000, slippage_bps=bad)

    def test_dust_quote_that_rounds_to_zero_rejects(self, executor):
        _install_router(executor, lambda routes: 1)

        result = executor.execute_swap(WETH, USDC, 10, executor.account.address)

        assert result["success"] is False
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()


class TestDeadlineAndGas:
    def test_deadline_passed_to_router(self, executor):
        router, _ = _install_router(executor, _best_direct_volatile)

        executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        deadline = router.functions.swapExactTokensForTokens.call_args[0][4]
        assert deadline == BLOCK_TS + TX_DEADLINE_SECONDS

    def test_eip1559_fees_and_estimated_gas_limit(self, executor):
        _, swap_fn = _install_router(executor, _best_direct_volatile)

        executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        params = swap_fn.build_transaction.call_args[0][0]
        assert "gasPrice" not in params
        assert params["maxPriorityFeePerGas"] == PRIORITY_FEE
        assert params["maxFeePerGas"] == 2 * BASE_FEE + PRIORITY_FEE
        assert params["gas"] == GAS_ESTIMATE * 125 // 100
        assert params["chainId"] == 8453

    def test_priority_fee_clamped_to_ceiling(self, executor):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        executor.trusted_w3.eth.max_priority_fee = 1000 * 10**9

        executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        params = swap_fn.build_transaction.call_args[0][0]
        assert params["maxPriorityFeePerGas"] == MAX_PRIORITY_FEE_WEI == 10**8
        assert params["maxFeePerGas"] == 2 * BASE_FEE + MAX_PRIORITY_FEE_WEI

    def test_simulation_revert_aborts_before_broadcast(self, executor):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        swap_fn.estimate_gas.side_effect = ContractLogicError("execution reverted: InsufficientOutputAmount")

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is False
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()

    def test_wrong_chain_rejected(self, executor):
        _install_router(executor, _best_direct_volatile)
        executor.trusted_w3.eth.chain_id = 1

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is False
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()


class TestNonceHandling:
    def test_nonce_from_pending_state(self, executor, nonces):
        nonces["pending"] = nonces["latest"] = 42
        _, swap_fn = _install_router(executor, _best_direct_volatile)

        executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert swap_fn.build_transaction.call_args[0][0]["nonce"] == 42
        blocks = [c.args[1] for c in executor.trusted_w3.eth.get_transaction_count.call_args_list]
        assert "pending" in blocks

    def test_sequential_swaps_do_not_reuse_nonce_when_rpc_lags(self, executor):
        _, swap_fn = _install_router(executor, _best_direct_volatile)

        executor.execute_swap(WETH, USDC, 10**17, executor.account.address)
        executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        used = [c.args[0]["nonce"] for c in swap_fn.build_transaction.call_args_list]
        assert used == [7, 8]

    def test_approval_then_swap_use_consecutive_nonces_and_requote(self, executor):
        router, swap_fn = _install_router(executor, _best_direct_volatile)
        token = _install_token(executor, allowance=0)

        result = executor.execute_swap(USDC, WETH, 10 * 10**6, executor.account.address)

        assert result["success"] is True
        approve_fn = token.functions.approve.return_value
        assert approve_fn.build_transaction.call_args[0][0]["nonce"] == 7
        assert swap_fn.build_transaction.call_args[0][0]["nonce"] == 8
        assert router.functions.getAmountsOut.call_count == 2 * len(executor._candidate_routes(USDC, WETH))

    def test_stale_floor_resyncs_when_last_tx_vanished(self, executor, nonces):
        executor._next_nonce, executor._last_sent_hash = 9, b"\x01" * 32
        executor.trusted_w3.eth.get_transaction.side_effect = TransactionNotFound("gone")

        assert executor._get_nonce() == nonces["pending"] == 7
        assert executor._next_nonce is None

    def test_floor_kept_while_last_tx_still_known(self, executor):
        executor._next_nonce, executor._last_sent_hash = 9, b"\x01" * 32

        assert executor._get_nonce() == 9

    def test_concurrent_sends_never_share_a_nonce(self, executor):
        def slow_contract_fn():
            fn = MagicMock()
            fn.estimate_gas.side_effect = lambda *a, **k: (time.sleep(0.05), GAS_ESTIMATE)[1]
            fn.build_transaction.side_effect = lambda params: {**params, "to": Web3.to_checksum_address(AERODROME_ROUTER_ADDRESS), "data": "0x"}
            return fn
        fns = [slow_contract_fn() for _ in range(4)]
        threads = [threading.Thread(target=executor._send_contract_tx, args=(fn,)) for fn in fns]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        used = sorted(fn.build_transaction.call_args[0][0]["nonce"] for fn in fns)
        assert used == [7, 8, 9, 10]

    def test_rpc_nonce_wins_when_ahead_of_local_tracker(self, executor, nonces):
        executor._next_nonce = 3
        nonces["pending"] = 9

        assert executor._get_nonce() == 9

    def test_refuses_when_unconfirmed_tx_in_flight(self, executor, nonces):
        nonces["pending"], nonces["latest"] = 8, 7
        router, _ = _install_router(executor, _best_direct_volatile)

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is False
        assert "in flight" in result["error"]
        router.functions.swapExactTokensForTokens.assert_not_called()

    def test_concurrent_swaps_never_stack_behind_an_unconfirmed_tx(self, executor, nonces):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        swap_fn.estimate_gas.side_effect = lambda *a, **k: (time.sleep(0.05), GAS_ESTIMATE)[1]

        def broadcast(raw):
            nonces["pending"] += 1
            return Web3.keccak(raw)
        executor.trusted_w3.eth.send_raw_transaction.side_effect = broadcast
        results = []
        swap = lambda: results.append(executor.execute_swap(WETH, USDC, 10**17, executor.account.address))
        threads = [threading.Thread(target=swap) for _ in range(2)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert executor.trusted_w3.eth.send_raw_transaction.call_count == 1
        assert sorted(r["success"] for r in results) == [False, True]
        assert any("in flight" in r.get("error", "") for r in results)


class TestReceiptReconciliation:
    def test_timeout_then_mined_reports_success(self, executor):
        _install_router(executor, _best_direct_volatile)
        eth = executor.trusted_w3.eth
        eth.wait_for_transaction_receipt.side_effect = TimeExhausted("Transaction is not in the chain after 180 seconds")
        eth.get_transaction_receipt.return_value = AttributeDict({"status": 1})

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is True and result["confirmed"] is True
        assert eth.send_raw_transaction.call_count == 1

    def test_timeout_still_pending_is_flagged_not_resent(self, executor):
        _install_router(executor, _best_direct_volatile)
        eth = executor.trusted_w3.eth
        eth.wait_for_transaction_receipt.side_effect = TimeExhausted("not in the chain")
        eth.get_transaction_receipt.side_effect = TransactionNotFound("no receipt")
        eth.get_transaction.return_value = AttributeDict({"hash": b"x"})

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is False
        assert result["pending"] is True
        assert eth.send_raw_transaction.call_count == 1

    def test_timeout_and_dropped_resets_local_nonce(self, executor):
        _install_router(executor, _best_direct_volatile)
        eth = executor.trusted_w3.eth
        eth.wait_for_transaction_receipt.side_effect = TimeExhausted("not in the chain")
        eth.get_transaction_receipt.side_effect = TransactionNotFound("no receipt")
        eth.get_transaction.side_effect = TransactionNotFound("unknown")

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is False and result["error"] == "transaction dropped"
        assert executor._next_nonce == 7
        assert executor._last_sent_hash is None

    def test_timeout_with_unknown_status_flags_pending_and_resyncs(self, executor, nonces):
        _install_router(executor, _best_direct_volatile)
        eth = executor.trusted_w3.eth
        eth.wait_for_transaction_receipt.side_effect = TimeExhausted("not in the chain")
        eth.get_transaction_receipt.side_effect = Exception("upstream 502")
        nonces["pending"] = nonces["latest"] = 8

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["pending"] is True
        assert executor._next_nonce == 8

    def test_broadcast_error_for_tx_the_node_has_is_not_resigned(self, executor):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        eth = executor.trusted_w3.eth
        eth.send_raw_transaction.side_effect = Exception("nonce too low")
        eth.get_transaction.return_value = AttributeDict({"hash": b"x"})

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["success"] is True
        assert swap_fn.build_transaction.call_count == 1


class TestExactApproval:
    ROUTER = Web3.to_checksum_address(AERODROME_ROUTER_ADDRESS)
    AMOUNT = 10 * 10**6

    def _buy(self, executor):
        return executor.execute_swap(USDC, WETH, self.AMOUNT, executor.account.address)

    def test_approves_exactly_the_sell_amount_never_max(self, executor):
        _install_router(executor, _best_direct_volatile)
        token = _install_token(executor, allowance=0)

        assert self._buy(executor)["success"] is True

        token.functions.approve.assert_called_once_with(self.ROUTER, self.AMOUNT)

    def test_nonzero_shortfall_is_reset_to_zero_first(self, executor):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        token = _install_token(executor, allowance=5)

        assert self._buy(executor)["success"] is True

        assert [c.args for c in token.functions.approve.call_args_list] == [(self.ROUTER, 0), (self.ROUTER, self.AMOUNT)]
        assert [c.args[0]["nonce"] for c in token.functions.approve.return_value.build_transaction.call_args_list] == [7, 8]
        assert swap_fn.build_transaction.call_args[0][0]["nonce"] == 9

    @pytest.mark.parametrize("existing", [AMOUNT, AMOUNT * 3])
    def test_existing_allowance_at_or_above_amount_is_reused_without_a_tx(self, executor, existing):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        token = _install_token(executor, allowance=existing)

        assert self._buy(executor)["success"] is True

        token.functions.approve.assert_not_called()
        assert executor.trusted_w3.eth.send_raw_transaction.call_count == 1
        assert swap_fn.build_transaction.call_args[0][0]["nonce"] == 7

    def test_no_allowance_is_remembered_between_swaps(self, executor):
        _install_router(executor, _best_direct_volatile)
        token = _install_token(executor, allowance=0)

        self._buy(executor)
        self._buy(executor)

        assert token.functions.allowance.return_value.call.call_count == 2
        assert [c.args for c in token.functions.approve.call_args_list] == [(self.ROUTER, self.AMOUNT)] * 2

    @pytest.mark.parametrize("allowance", [0, 5])
    def test_unconfirmed_approval_or_reset_aborts_without_a_swap(self, executor, allowance):
        router, swap_fn = _install_router(executor, _best_direct_volatile)
        _install_token(executor, allowance=allowance)
        executor.trusted_w3.eth.wait_for_transaction_receipt.side_effect = [AttributeDict({"status": 0})]

        result = self._buy(executor)

        assert result == {"success": False, "error": "approval failed"}
        swap_fn.build_transaction.assert_not_called()
        assert executor.trusted_w3.eth.send_raw_transaction.call_count == 1

    def test_kill_switch_engaged_after_approval_blocks_the_swap(self, executor, tmp_path):
        _, swap_fn = _install_router(executor, _best_direct_volatile)
        _install_token(executor, allowance=0)

        def approval_mined(*args, **kwargs):
            (tmp_path / "kill").touch()
            return AttributeDict({"status": 1, "gasUsed": 50_000})
        executor.trusted_w3.eth.wait_for_transaction_receipt.side_effect = approval_mined

        result = self._buy(executor)

        assert result == {"success": False, "error": "kill switch engaged"}
        assert executor.trusted_w3.eth.send_raw_transaction.call_count == 1

    def test_kill_switch_is_checked_before_any_allowance_read(self, executor, tmp_path):
        _install_router(executor, _best_direct_volatile)
        token = _install_token(executor, allowance=0)
        (tmp_path / "kill").touch()

        assert self._buy(executor) == {"success": False, "error": "kill switch engaged"}
        token.functions.approve.assert_not_called()


class TestKillSwitchAndCap:
    def test_kill_switch_env_blocks_swap(self, executor, monkeypatch):
        monkeypatch.setenv("ONCHAIN_KILL_SWITCH", "true")
        router, _ = _install_router(executor, _best_direct_volatile)

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result == {"success": False, "error": "kill switch engaged"}
        router.functions.getAmountsOut.assert_not_called()

    def test_kill_switch_file_blocks_swap(self, executor, tmp_path):
        (tmp_path / "kill").touch()
        _install_router(executor, _best_direct_volatile)

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result["error"] == "kill switch engaged"
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()

    def test_kill_switch_engaged_mid_flight_blocks_signing(self, executor, tmp_path):
        _, swap_fn = _install_router(executor, _best_direct_volatile)

        def engage_during_simulation(*args, **kwargs):
            (tmp_path / "kill").touch()
            return GAS_ESTIMATE
        swap_fn.estimate_gas.side_effect = engage_during_simulation

        result = executor.execute_swap(WETH, USDC, 10**17, executor.account.address)

        assert result == {"success": False, "error": "kill switch engaged"}
        executor.trusted_w3.eth.send_raw_transaction.assert_not_called()

    def test_buy_above_cap_rejected(self, executor, monkeypatch):
        monkeypatch.setattr(eth_mod, "MAX_SWAP_USD", eth_mod.Decimal("100"))
        router, _ = _install_router(executor, _best_direct_volatile)

        result = executor.execute_swap(USDC, WETH, 101 * 10**6, executor.account.address)

        assert result["error"] == "position size cap exceeded"
        router.functions.getAmountsOut.assert_not_called()

    def test_buy_at_cap_and_sells_are_allowed(self, executor, monkeypatch):
        monkeypatch.setattr(eth_mod, "MAX_SWAP_USD", eth_mod.Decimal("100"))
        _install_router(executor, _best_direct_volatile)

        assert executor.execute_swap(USDC, WETH, 100 * 10**6, executor.account.address)["success"] is True
        assert executor.execute_swap(WETH, USDC, 10**20, executor.account.address)["success"] is True


class TestReferencePriceGuard:
    def test_buy_within_deviation_proceeds(self, executor):
        _install_router(executor, lambda routes: 4 * 10**16)  # 100 USDC -> 0.04 WETH = $2500

        result = executor.execute_swap(USDC, WETH, 100 * 10**6, executor.account.address, reference_price=2500)

        assert result["success"] is True

    def test_buy_from_thin_pool_rejected(self, executor):
        router, _ = _install_router(executor, lambda routes: 4 * 10**16)

        result = executor.execute_swap(USDC, WETH, 100 * 10**6, executor.account.address, reference_price=2400)

        assert result == {"success": False, "error": "quote deviates from reference price"}
        router.functions.swapExactTokensForTokens.assert_not_called()

    def test_sell_deviation_boundary(self, executor):
        _install_router(executor, lambda routes: 2450 * 10**6)

        assert executor.execute_swap(WETH, USDC, 10**18, executor.account.address, reference_price=2500)["success"] is True
        assert executor.execute_swap(WETH, USDC, 10**18, executor.account.address, reference_price=2600)["success"] is False

    def test_market_order_fallback_reuses_limit_order_reference(self, executor):
        router, _ = _install_router(executor, lambda routes: 2400 * 10**6)

        assert executor.place_limit_order("ETH-USDC", "SELL", 2500, amount_base_currency=1.0)["success"] is False
        assert executor.place_market_order("ETH-USDC", "SELL", amount_base_currency=1.0)["success"] is False
        router.functions.swapExactTokensForTokens.assert_not_called()


class TestPlaceMarketOrder:
    def test_buy_routes_usdc_into_token_swap(self, executor):
        router, _ = _install_router(executor, lambda routes: 2 * 10**16)  # 50 USDC -> 0.02 WETH

        result = executor.place_market_order("ETH-USDC", "BUY", amount_quote_currency=50, reference_price=2500)

        assert result["success"] is True
        amount_in, _, routes, _, _ = router.functions.swapExactTokensForTokens.call_args[0]
        assert amount_in == 50 * 10**6
        assert routes[0][0] == Web3.to_checksum_address(USDC)
        assert routes[-1][1] == Web3.to_checksum_address(WETH)

    def test_live_market_order_without_reference_price_refused(self, executor):
        router, _ = _install_router(executor, lambda routes: 2400 * 10**6)

        result = executor.place_market_order("ETH-USDC", "SELL", amount_base_currency=1.0)

        assert result["success"] is False and "reference price" in result["error"]
        router.functions.getAmountsOut.assert_not_called()

    def test_paper_mode_never_touches_chain(self, executor):
        executor.trading_mode = "paper"
        router, _ = _install_router(executor, _best_direct_volatile)

        assert executor.execute_swap(WETH, USDC, 10**17, executor.account.address)["tx_hash"] == "paper"
        router.functions.getAmountsOut.assert_not_called()
