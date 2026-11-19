"""CoW Protocol route: we sign our own fill-or-kill GPv2 sell order; solvers settle it."""
import logging
import time

from eth_abi import encode as abi_encode
from web3 import Web3

from ..cow_client import CowError
from .common import KillSwitchEngaged, _same_address, onchain_kill_switch_engaged

# CoW Protocol on Base. Settlement.vaultRelayer() and domainSeparator() were read on chain and
# match these constants (see tests).
COW_SETTLEMENT_ADDRESS = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
COW_VAULT_RELAYER_ADDRESS = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"
COW_APP_DATA = "{}"
COW_ORDER_VALIDITY_SECONDS = 600
COW_POLL_SECONDS = 5
COW_ORDER_TYPEHASH = Web3.keccak(text=(
    "Order(address sellToken,address buyToken,address receiver,uint256 sellAmount,uint256 buyAmount,"
    "uint32 validTo,bytes32 appData,uint256 feeAmount,string kind,bool partiallyFillable,"
    "string sellTokenBalance,string buyTokenBalance)"))
COW_DOMAIN_SEPARATOR = Web3.keccak(abi_encode(
    ["bytes32", "bytes32", "bytes32", "uint256", "address"],
    [Web3.keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
     Web3.keccak(text="Gnosis Protocol"), Web3.keccak(text="v2"), 8453, COW_SETTLEMENT_ADDRESS]))


def cow_order_digest(order):
    """EIP-712 digest of a GPv2 order, as GPv2Settlement verifies it."""
    struct = abi_encode(
        ["bytes32", "address", "address", "address", "uint256", "uint256", "uint32", "bytes32", "uint256",
         "bytes32", "bool", "bytes32", "bytes32"],
        [COW_ORDER_TYPEHASH, order["sellToken"], order["buyToken"], order["receiver"], order["sellAmount"],
         order["buyAmount"], order["validTo"], bytes.fromhex(order["appData"][2:]), order["feeAmount"],
         Web3.keccak(text=order["kind"]), order["partiallyFillable"], Web3.keccak(text=order["sellTokenBalance"]),
         Web3.keccak(text=order["buyTokenBalance"])])
    return Web3.keccak(b"\x19\x01" + COW_DOMAIN_SEPARATOR + Web3.keccak(struct))


class CowMixin:
    def _execute_cow_swap(self, token_in, token_out, amount_in, recipient, reference_price):
        """Sell order on CoW Protocol. We sign our own fill-or-kill GPv2 order (receiver us, limit =
        quote less SLIPPAGE_BPS, valid COW_ORDER_VALIDITY_SECONDS), so no third-party calldata is
        signed. Waits for it to settle; an expired order moved no funds and its allowance is reset."""
        refusal = self._api_swap_refusal(recipient, reference_price, "CoW")
        if refusal:
            return refusal
        try:
            refusal = self._preflight(token_in, amount_in)
            if refusal:
                return refusal

            with self._nonce_lock:
                if self._has_unconfirmed_tx():
                    return self._in_flight_refusal()
                quote_id, quoted_out = self._cow_quote(token_in, token_out, amount_in)
                refusal = self._reference_price_refusal(
                    token_in, token_out, amount_in, quoted_out, reference_price, "CoW")
                if refusal:
                    return {"success": False, "error": refusal}
                our_min = self._amount_out_minimum(quoted_out)
                if self._approve_token(token_in, amount_in, spender=COW_VAULT_RELAYER_ADDRESS) is None:
                    logging.error(f"Token approval failed for {token_in}, cannot place order")
                    return {"success": False, "error": "approval failed"}

            before = self._token_balance(token_out, "latest")
            order = self._cow_order(token_in, token_out, amount_in, our_min,
                                    self._latest_block()["timestamp"] + COW_ORDER_VALIDITY_SECONDS)
            if onchain_kill_switch_engaged():
                raise KillSwitchEngaged("kill switch engaged")
            signature = self._sign_cow_order(order)
            uid = self.cow.post_order({
                **{k: str(v) if k in ("sellAmount", "buyAmount", "feeAmount") else v for k, v in order.items()},
                "appData": COW_APP_DATA, "appDataHash": order["appData"], "signingScheme": "eip712",
                "signature": signature, "from": self.account.address, "quoteId": quote_id})
            logging.info(f"CoW order {uid}: sell={amount_in} quoted={quoted_out} limit={our_min} validTo={order['validTo']}")
        except CowError as e:
            logging.error(f"CoW order refused; nothing placed: {e}")
            return {"success": False, "error": f"CoW: {e}"}
        except Exception as e:
            logging.error(f"CoW order not placed: {e}")
            return {"success": False, "error": str(e)}

        base = {"order_uid": uid, "quoted_out": quoted_out, "min_out": our_min, "gas_wei": 0}
        status = self._await_cow_order(uid)
        if status in ("expired", "cancelled"):
            self._reset_allowance(token_in, COW_VAULT_RELAYER_ADDRESS)
            return {**base, "success": False, "error": f"CoW order {status}; nothing filled"}
        if status != "fulfilled":
            logging.error(f"CoW order {uid} unresolved ({status}); NOT resubmitting")
            return {**base, "success": False, "pending": True, "error": f"CoW order {status}; do not resubmit"}

        trade = (self.cow.trades(uid) or [{}])[0]
        received = self._token_balance(token_out, trade.get("blockNumber") or "latest") - before
        self._check_received(token_out, received, our_min, uid)
        return {**base, "success": True, "confirmed": True, "tx_hash": trade.get("txHash"), "received": received}

    def _cow_quote(self, token_in, token_out, amount_in):
        """Returns (quote id, buyAmount net of network costs) after checking the quote echoes our order."""
        response = self.cow.quote({
            "sellToken": token_in, "buyToken": token_out, "from": self.account.address,
            "receiver": self.account.address, "kind": "sell", "sellAmountBeforeFee": str(amount_in),
            "appData": COW_APP_DATA, "signingScheme": "eip712", "priceQuality": "optimal"})
        try:
            quote = response["quote"]
            echoes = (_same_address(quote["sellToken"], token_in) and _same_address(quote["buyToken"], token_out)
                      and _same_address(quote["receiver"], self.account.address) and quote["kind"] == "sell"
                      and quote["partiallyFillable"] is False
                      and int(quote["sellAmount"]) + int(quote["feeAmount"]) == amount_in)
            quoted_out = int(quote["buyAmount"])
            quote_id = response["id"]
        except (KeyError, TypeError, ValueError) as e:
            raise CowError(f"malformed quote: {e!r}") from None
        if not echoes or quoted_out <= 0:
            raise CowError(f"quote does not match our order: {quote}")
        return quote_id, quoted_out

    def _cow_order(self, token_in, token_out, amount_in, buy_amount, valid_to):
        return {"sellToken": Web3.to_checksum_address(token_in), "buyToken": Web3.to_checksum_address(token_out),
                "receiver": self.account.address, "sellAmount": amount_in, "buyAmount": buy_amount,
                "validTo": valid_to, "appData": "0x" + Web3.keccak(text=COW_APP_DATA).hex().removeprefix("0x"),
                "feeAmount": 0, "kind": "sell", "partiallyFillable": False,
                "sellTokenBalance": "erc20", "buyTokenBalance": "erc20"}

    def _sign_cow_order(self, order):
        signed = self.account.unsafe_sign_hash(cow_order_digest(order))
        return "0x" + signed.signature.hex().removeprefix("0x")

    def _await_cow_order(self, uid):
        """Poll until the order is fulfilled, expired or cancelled; returns the last status seen."""
        status = None
        for _ in range((COW_ORDER_VALIDITY_SECONDS + 120) // COW_POLL_SECONDS):
            try:
                status = self.cow.order(uid).get("status")
            except CowError as e:
                logging.warning(f"CoW order {uid} status unavailable: {e}")
            if status in ("fulfilled", "expired", "cancelled"):
                return status
            time.sleep(COW_POLL_SECONDS)
        return status
