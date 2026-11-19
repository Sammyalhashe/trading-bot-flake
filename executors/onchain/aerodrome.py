"""Aerodrome (Solidly-style v2 AMM) route: on-chain quote and Router.swapExactTokensForTokens."""
import logging

from web3 import Web3

from .common import SLIPPAGE_BPS, TOKENS, TX_DEADLINE_SECONDS, retry_rpc_call

# Verified on Base mainnet via eth_call: Router.defaultFactory() == PoolFactory, selectors
# 0x5509a1ac / 0xcac88ea9 present, and deadline/minOut reverts confirmed by simulation.
AERODROME_ROUTER_ADDRESS = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERODROME_POOL_FACTORY_ADDRESS = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

_AERODROME_ROUTES_PARAM = {
    "components": [
        {"internalType": "address", "name": "from", "type": "address"},
        {"internalType": "address", "name": "to", "type": "address"},
        {"internalType": "bool", "name": "stable", "type": "bool"},
        {"internalType": "address", "name": "factory", "type": "address"}
    ],
    "internalType": "struct IRouter.Route[]",
    "name": "routes",
    "type": "tuple[]"
}

AERODROME_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            _AERODROME_ROUTES_PARAM
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            _AERODROME_ROUTES_PARAM,
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


class AerodromeMixin:
    def _candidate_routes(self, token_in, token_out):
        factory = Web3.to_checksum_address(AERODROME_POOL_FACTORY_ADDRESS)
        a = Web3.to_checksum_address(token_in)
        b = Web3.to_checksum_address(token_out)
        weth = Web3.to_checksum_address(TOKENS["WETH"])
        candidates = [[(a, b, stable, factory)] for stable in (False, True)]
        if weth not in (a, b):
            candidates += [
                [(a, weth, stable_in, factory), (weth, b, stable_out, factory)]
                for stable_in in (False, True)
                for stable_out in (False, True)
            ]
        return candidates

    def get_quote(self, token_in, token_out, amount_in):
        """Best Aerodrome route by Router.getAmountsOut on the trusted RPC.

        Returns (routes, expected_amount_out) or None. Non-existent pools quote 0 on Aerodrome,
        so zero outputs are discarded rather than treated as a price.
        """
        best = None
        for routes in self._candidate_routes(token_in, token_out):
            try:
                amounts = retry_rpc_call(
                    lambda: self.router_contract.functions.getAmountsOut(amount_in, routes).call()
                )
            except Exception as e:
                logging.debug(f"getAmountsOut failed for {routes}: {e}")
                continue
            amount_out = amounts[-1]
            if amount_out > 0 and (best is None or amount_out > best[1]):
                best = (routes, amount_out)
        if best is None:
            logging.warning(f"No Aerodrome quote for {amount_in} of {token_in} -> {token_out}")
        return best

    def _execute_aerodrome_swap(self, token_in, token_out, amount_in, recipient, reference_price):
        try:
            refusal = self._preflight(token_in, amount_in)
            if refusal:
                return refusal

            # Held from the in-flight check through broadcast so concurrent swaps cannot both pass it.
            with self._nonce_lock:
                if self._has_unconfirmed_tx():
                    return self._in_flight_refusal()

                quote = self.get_quote(token_in, token_out, amount_in)
                if quote is None:
                    return {"success": False, "error": "no quote available; refusing to swap without slippage protection"}

                approval_result = self._approve_token(token_in, amount_in)
                if approval_result is None:
                    logging.error(f"Token approval failed for {token_in}, cannot execute swap")
                    return {"success": False, "error": "approval failed"}
                if approval_result is not True:
                    quote = self.get_quote(token_in, token_out, amount_in)
                    if quote is None:
                        return {"success": False, "error": "no quote available after approval"}

                routes, expected_out = quote
                if reference_price is not None:
                    refusal = self._reference_price_refusal(token_in, token_out, amount_in, expected_out, reference_price, "Aerodrome")
                    if refusal:
                        return {"success": False, "error": refusal}

                amount_out_min = self._amount_out_minimum(expected_out)
                if amount_out_min <= 0:
                    return {"success": False, "error": "amountOutMinimum rounds to zero; refusing swap"}

                deadline = self._latest_block()["timestamp"] + TX_DEADLINE_SECONDS
                logging.info(
                    f"Aerodrome swap: in={amount_in} expected={expected_out} min={amount_out_min} "
                    f"({SLIPPAGE_BPS}bps) hops={len(routes)} deadline={deadline}"
                )
                swap_fn = self.router_contract.functions.swapExactTokensForTokens(
                    amount_in, amount_out_min, routes, Web3.to_checksum_address(recipient), deadline
                )
                tx_hash = self._send_contract_tx(swap_fn)
        except Exception as e:
            logging.error(f"Swap not broadcast: {e}")
            return {"success": False, "error": str(e)}

        return self._await_swap(tx_hash, amount_in, amount_out_min)[0]
