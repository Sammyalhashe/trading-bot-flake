"""KyberSwap aggregator route: API-built router calldata, validated before signing."""
import logging

from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError

from ..kyberswap_client import KyberSwapError
from .common import SLIPPAGE_BPS, _RawContractCall, _receipt_fee_wei, _same_address

# KyberSwap MetaAggregationRouterV2 (same address on all chains; code checked on Base). Only its
# swap(SwapExecutionParams) entry point is accepted; the router enforces desc.minReturnAmount.
KYBER_ROUTER_ADDRESS = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
KYBER_SWAP_SELECTOR = "0xe21fd0e9"
KYBER_SWAP_TYPES = ["(address,address,bytes,(address,address,address[],uint256[],address[],uint256[],"
                    "address,uint256,uint256,uint256,bytes),bytes)"]


class KyberSwapMixin:
    def _execute_kyberswap_swap(self, token_in, token_out, amount_in, recipient, reference_price):
        """Exact-input swap with KyberSwap router calldata, validated before signing (see
        _check_kyber_build) and refused when the quote is far from reference_price."""
        refusal = self._api_swap_refusal(recipient, reference_price, "KyberSwap")
        if refusal:
            return refusal
        try:
            refusal = self._preflight(token_in, amount_in)
            if refusal:
                return refusal

            with self._nonce_lock:
                if self._has_unconfirmed_tx():
                    return self._in_flight_refusal()

                route = self.kyber.routes(token_in, token_out, amount_in)
                refusal = self._reference_price_refusal(
                    token_in, token_out, amount_in, int(route["amountOut"]), reference_price, "KyberSwap")
                if refusal:
                    return {"success": False, "error": refusal}

                if self._approve_token(token_in, amount_in, spender=KYBER_ROUTER_ADDRESS) is None:
                    logging.error(f"Token approval failed for {token_in}, cannot execute swap")
                    return {"success": False, "error": "approval failed"}

                built = self.kyber.build(route, self.account.address, self.account.address, SLIPPAGE_BPS)
                quoted_out = int(built["amountOut"])
                refusal = self._reference_price_refusal(
                    token_in, token_out, amount_in, quoted_out, reference_price, "KyberSwap")
                if refusal:
                    return {"success": False, "error": refusal}
                our_min = self._amount_out_minimum(quoted_out)
                self._check_kyber_build(built, token_in, token_out, amount_in, our_min)
                self._require_code(KYBER_ROUTER_ADDRESS)
                logging.info(f"KyberSwap swap: in={amount_in} quoted={quoted_out} ourMin={our_min} gas={built.get('gas')}")
                tx_hash = self._send_contract_tx(_RawContractCall(
                    self.trusted_w3, KYBER_ROUTER_ADDRESS, built["data"], int(built.get("gas") or 0) or None))
        except KyberSwapError as e:
            logging.error(f"KyberSwap route refused; nothing signed: {e}")
            return {"success": False, "error": f"KyberSwap: {e}"}
        except Exception as e:
            logging.error(f"Swap not broadcast: {e}")
            return {"success": False, "error": str(e)}

        result, receipt = self._await_swap(tx_hash, amount_in, our_min)
        result.update(quoted_out=quoted_out, min_out=our_min, gas_wei=_receipt_fee_wei(receipt))
        if result.get("confirmed"):
            result["received"] = self._verify_fill(token_out, receipt, our_min, result["tx_hash"])
        return result

    def _check_kyber_build(self, built, token_in, token_out, amount_in, our_min):
        """Require the pinned router's swap() with our tokens, amount and receiver, no fee receivers
        or permit, no ETH value, and minReturnAmount at or above our floor."""
        try:
            router, value, data = built["routerAddress"], int(built.get("transactionValue") or 0), built["data"]
        except (KeyError, TypeError, ValueError) as e:
            raise KyberSwapError(f"malformed build response: {e!r}") from None
        if not _same_address(router, KYBER_ROUTER_ADDRESS):
            raise KyberSwapError(f"routerAddress {router} is not the pinned router")
        if value != 0:
            raise KyberSwapError(f"transactionValue {value} must be 0 for an ERC-20 sell")
        if not data.lower().startswith(KYBER_SWAP_SELECTOR):
            raise KyberSwapError("calldata is not MetaAggregationRouterV2.swap")
        try:
            (_, _, _, desc, _), = abi_decode(KYBER_SWAP_TYPES, bytes.fromhex(data[10:]))
            src, dst, _, src_amounts, fee_receivers, fee_amounts, receiver, amount, min_return, _, permit = desc
        except (DecodingError, ValueError, TypeError) as e:
            raise KyberSwapError(f"malformed calldata: {e!r}") from None
        if not (_same_address(src, token_in) and _same_address(dst, token_out) and amount == amount_in):
            raise KyberSwapError(f"desc {src}->{dst} amount {amount} does not match our order")
        if sum(src_amounts) > amount_in:
            raise KyberSwapError(f"srcAmounts {src_amounts} exceed our amount {amount_in}")
        if fee_receivers or any(fee_amounts):
            raise KyberSwapError(f"fee receivers present: {fee_receivers} {fee_amounts}")
        if permit:
            raise KyberSwapError("permit present; we approve explicitly")
        if not _same_address(receiver, self.account.address):
            raise KyberSwapError(f"dstReceiver {receiver} is not our wallet")
        if min_return < our_min:
            raise KyberSwapError(f"minReturnAmount {min_return} below our floor {our_min}")
