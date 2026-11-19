"""0x Swap API v2 route (AllowanceHolder flow): 0x is an off-chain quote source only."""
import logging
import os
from decimal import Decimal

from eth_abi import decode as abi_decode
from eth_abi.exceptions import DecodingError
from web3 import Web3

from ..zerox_client import ZeroExError
from .common import EXPECTED_CHAIN_ID, SLIPPAGE_BPS, _RawContractCall, _same_address, retry_rpc_call

# Pinned per the 0x-settler README (Cancun chains incl. Base). Settler itself is never pinned:
# it is read from the registry's taker-submitted feature before every swap.
ALLOWANCE_HOLDER_ADDRESS = "0x0000000000001fF3684f28c67538d4D072C22734"
ALLOWANCE_HOLDER_EXEC_SELECTOR = "0x2213bc0b"  # exec(address,address,uint256,address,bytes)
# ISettlerTakerSubmitted.execute(AllowedSlippage(recipient, buyToken, minAmountOut), bytes[] actions, bytes32)
SETTLER_EXECUTE_SELECTOR = "0x1fff991f"
SETTLER_REGISTRY_ADDRESS = "0x00000000000004533Fe15556B1E086BB1A72cEae"
SETTLER_TAKER_SUBMITTED_FEATURE = 2
SETTLER_REGISTRY_ABI = [
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "featureId", "type": "uint128"}], "name": "prev", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]
ZEROX_MAX_QUOTE_AGE_BLOCKS = int(os.environ.get("ZEROX_MAX_QUOTE_AGE_BLOCKS", "3"))


class ZeroExQuoteRejected(ZeroExError):
    pass


class ZeroExMixin:
    def _execute_zerox_swap(self, token_in, token_out, amount_in, recipient, reference_price):
        """Exact-input swap with 0x AllowanceHolder calldata, signed and sent by us.

        Refuses unless the signed /quote targets the pinned AllowanceHolder, exec()s into the
        registry's current (or, during dwell, previous) Settler for our sell token and amount,
        sends no ETH, sets minBuyAmount at or above our slippage floor, was sampled within
        ZEROX_MAX_QUOTE_AGE_BLOCKS, and fills within MAX_PRICE_DEVIATION_PCT of reference_price.
        Failures attributable to 0x carry "zerox_unavailable" so execute_swap can fall back.
        """
        if not _same_address(recipient, self.account.address):
            return {"success": False, "error": "0x swaps must pay our own wallet"}
        if reference_price is None or Decimal(str(reference_price)) <= 0:
            logging.error(f"No usable reference price ({reference_price}); refusing unguarded 0x swap")
            return {"success": False, "error": "no reference price; refusing unguarded swap"}
        try:
            refusal = self._preflight(token_in, amount_in)
            if refusal:
                return refusal

            with self._nonce_lock:
                if self._has_unconfirmed_tx():
                    return self._in_flight_refusal()

                indicative = self._zerox_request("price", token_in, token_out, amount_in)
                self._check_zerox_response(indicative, token_in, token_out, amount_in, reference_price, firm=False)

                if self._approve_token(token_in, amount_in, spender=ALLOWANCE_HOLDER_ADDRESS) is None:
                    logging.error(f"Token approval failed for {token_in}, cannot execute swap")
                    return {"success": False, "error": "approval failed"}

                quote = self._zerox_request("quote", token_in, token_out, amount_in)
                our_min = self._check_zerox_response(quote, token_in, token_out, amount_in, reference_price, firm=True)
                swap_call = self._check_zerox_transaction(quote, token_in, token_out, amount_in, our_min)
                self._check_zerox_freshness(quote)
                logging.info(
                    f"0x swap: in={amount_in} buy={quote['buyAmount']} minBuy={quote['minBuyAmount']} "
                    f"ourMin={our_min} zeroExFee={(quote.get('fees') or {}).get('zeroExFee')} "
                    f"route={[f.get('source') for f in (quote.get('route') or {}).get('fills', [])]} zid={quote.get('zid')}"
                )
                tx_hash = self._send_contract_tx(swap_call)
        except ZeroExError as e:
            logging.error(f"0x route refused; nothing signed: {e}")
            return {"success": False, "error": f"0x: {e}", "zerox_unavailable": True}
        except Exception as e:
            logging.error(f"Swap not broadcast: {e}")
            return {"success": False, "error": str(e)}

        result, receipt = self._await_swap(tx_hash, amount_in, our_min)
        if result.get("confirmed"):
            self._verify_fill(token_out, receipt, our_min, result["tx_hash"])
        return result

    def _zerox_request(self, kind, token_in, token_out, amount_in):
        if self.zerox is None:
            raise ZeroExError("ZEROX_API_KEY is not set")
        params = {
            "chainId": EXPECTED_CHAIN_ID,
            "sellToken": token_in,
            "buyToken": token_out,
            "sellAmount": str(amount_in),
            "taker": self.account.address,
            "recipient": self.account.address,
            "slippagePpm": SLIPPAGE_BPS * 100,
        }
        return getattr(self.zerox, kind)(params)

    def _check_zerox_response(self, response, token_in, token_out, amount_in, reference_price, firm):
        """Validate a /price (firm=False) or /quote (firm=True) body. Returns our minimum output."""
        try:
            issues = response["issues"]
            if response.get("liquidityAvailable") is not True:
                raise ZeroExQuoteRejected("no liquidity available")
            if issues.get("balance") is not None:
                raise ZeroExQuoteRejected(f"insufficient sell-token balance: {issues['balance']}")
            allowance_issue = issues.get("allowance")
            if allowance_issue is not None and not _same_address(allowance_issue.get("spender"), ALLOWANCE_HOLDER_ADDRESS):
                raise ZeroExQuoteRejected(f"allowance spender {allowance_issue.get('spender')} is not AllowanceHolder")
            if not _same_address(response.get("allowanceTarget"), ALLOWANCE_HOLDER_ADDRESS):
                raise ZeroExQuoteRejected(f"allowanceTarget {response.get('allowanceTarget')} is not AllowanceHolder")
            if not (_same_address(response.get("sellToken"), token_in) and _same_address(response.get("buyToken"), token_out)
                    and int(response["sellAmount"]) == amount_in):
                raise ZeroExQuoteRejected("response does not match the requested order")
            buy_amount, min_buy = int(response["buyAmount"]), int(response["minBuyAmount"])
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ZeroExQuoteRejected(f"malformed 0x response: {e!r}") from None
        if buy_amount <= 0 or min_buy <= 0:
            raise ZeroExQuoteRejected(f"non-positive buyAmount {buy_amount} / minBuyAmount {min_buy}")
        refusal = self._reference_price_refusal(token_in, token_out, amount_in, buy_amount, reference_price, "0x")
        if refusal:
            raise ZeroExQuoteRejected(refusal)
        our_min = self._amount_out_minimum(buy_amount)
        if firm:
            if issues.get("simulationIncomplete") is not False:
                raise ZeroExQuoteRejected("0x could not simulate the swap")
            if issues.get("invalidSourcesPassed"):
                raise ZeroExQuoteRejected(f"invalid sources: {issues['invalidSourcesPassed']}")
            if our_min <= 0 or min_buy < our_min:
                raise ZeroExQuoteRejected(f"minBuyAmount {min_buy} below our floor {our_min}")
        return our_min

    def _check_zerox_transaction(self, quote, token_in, token_out, amount_in, our_min):
        """Validate quote.transaction, including the Settler call it forwards, and return it as a
        call for _send_contract_tx."""
        try:
            tx = quote["transaction"]
            to, value, data = tx["to"], int(tx["value"]), tx["data"]
            api_gas = int(tx["gas"]) if tx.get("gas") else None
            if not data.lower().startswith(ALLOWANCE_HOLDER_EXEC_SELECTOR):
                raise ZeroExQuoteRejected("transaction.data is not AllowanceHolder.exec")
            operator, token, amount, target, settler_call = abi_decode(
                ["address", "address", "uint256", "address", "bytes"], bytes.fromhex(data[10:]))
            if "0x" + settler_call[:4].hex() != SETTLER_EXECUTE_SELECTOR:
                raise ZeroExQuoteRejected("exec() data is not Settler.execute")
            (recipient, buy_token, min_amount_out), _, _ = abi_decode(
                ["(address,address,uint256)", "bytes[]", "bytes32"], settler_call[4:])
        except (KeyError, TypeError, ValueError, AttributeError, DecodingError) as e:
            raise ZeroExQuoteRejected(f"malformed 0x transaction: {e!r}") from None
        if not _same_address(recipient, self.account.address):
            raise ZeroExQuoteRejected(f"Settler slippage recipient {recipient} is not our wallet")
        if not _same_address(buy_token, token_out):
            raise ZeroExQuoteRejected(f"Settler slippage buyToken {buy_token} is not {token_out}")
        if min_amount_out < our_min:
            raise ZeroExQuoteRejected(f"Settler slippage minAmountOut {min_amount_out} below our floor {our_min}")
        if not _same_address(to, ALLOWANCE_HOLDER_ADDRESS):
            raise ZeroExQuoteRejected(f"transaction.to {to} is not AllowanceHolder")
        if value != 0:
            raise ZeroExQuoteRejected(f"transaction.value {value} must be 0 for an ERC-20 sell")
        if not _same_address(token, token_in) or amount > amount_in:
            raise ZeroExQuoteRejected(f"exec() token/amount {token}/{amount} does not match our {token_in}/{amount_in}")
        if not _same_address(operator, target):
            raise ZeroExQuoteRejected(f"exec() operator {operator} differs from target {target}")
        self._require_allowance_holder_code()
        self._require_genuine_settler(target)
        return _RawContractCall(self.trusted_w3, ALLOWANCE_HOLDER_ADDRESS, data, api_gas)

    def _require_allowance_holder_code(self):
        if self._allowance_holder_verified:
            return
        code = retry_rpc_call(lambda: self.trusted_w3.eth.get_code(Web3.to_checksum_address(ALLOWANCE_HOLDER_ADDRESS)))
        if not code:
            raise ZeroExQuoteRejected("AllowanceHolder has no code on the trusted RPC's chain")
        self._allowance_holder_verified = True

    def _require_genuine_settler(self, target):
        registry = self.trusted_w3.eth.contract(
            address=Web3.to_checksum_address(SETTLER_REGISTRY_ADDRESS), abi=SETTLER_REGISTRY_ABI)
        feature = SETTLER_TAKER_SUBMITTED_FEATURE
        try:
            current = retry_rpc_call(lambda: registry.functions.ownerOf(feature).call())
        except Exception as e:
            raise ZeroExQuoteRejected(f"Settler paused or registry unreadable (ownerOf({feature}): {e})") from None
        if _same_address(current, target):
            return
        try:
            previous = retry_rpc_call(lambda: registry.functions.prev(feature).call())
        except Exception as e:
            raise ZeroExQuoteRejected(f"exec() target {target} is not Settler {current}; prev({feature}) failed: {e}") from None
        if not _same_address(previous, target):
            raise ZeroExQuoteRejected(f"exec() target {target} is neither Settler {current} nor previous {previous}")
        logging.info(f"0x targets the previous Settler {target} (registry dwell period)")

    def _check_zerox_freshness(self, quote):
        age = self._latest_block()["number"] - int(quote["blockNumber"])
        if age > ZEROX_MAX_QUOTE_AGE_BLOCKS:
            raise ZeroExQuoteRejected(f"quote sampled {age} blocks ago (max {ZEROX_MAX_QUOTE_AGE_BLOCKS})")
