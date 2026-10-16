"""
Pump bonding curve market model.

Integer math in base units (lamports for SOL, 10^-6 for tokens), matching what
the on-chain program records in its TradeEvents. Every formula here was checked
against recorded mainnet trades (tests/test_market.py); where the official
TypeScript SDK's quote differs from what the program does, this module follows
the program:

    buy, exact tokens   cost = floor(amount * vQ / (vT - amount)) + 1
                        fees = ceil(cost * bps / 10000), protocol and creator separately
    buy, exact quote in x0 = floor(spendable * 10000 / (10000 + total_bps))
                        fees = ceil(x0 * bps / 10000), protocol and creator separately
                        quote = x0 - max(0, x0 + fees - spendable)   (fees stay as computed on x0)
                        tokens = floor((quote - 1) * vT / (vQ + quote - 1))
                        (the SDK computes from (spendable - 1) instead and overstates
                        tokens on most buys)
                        If those tokens reach what is left on the curve, the buy takes all
                        of it at the exact-tokens cost and the rest of `spendable` stays with
                        the buyer. (Inferred from recorded trades: a buy whose quote equalled
                        the cost of everything left still got only `tokens` and left dust,
                        so the check is on `tokens`, not on the quote. Completing buys do not
                        record `spendable`, so this cannot be checked more directly.)
    sell                gross = floor(amount * vQ / (vT + amount))
                        fees = ceil(gross * bps / 10000); seller gets gross - fees

vQ / vT are the virtual quote / token reserves before the trade. Both virtual and
real reserves move by `quote` and `tokens`; fees never touch the reserves.

Legacy cashback coins (no new ones can be created) charge their creator fee as
a cashback fee instead, reported in the event's `cashback` field. On exact-quote-in
buys it is verified: it enters `total_bps` like any fee. On exact-token buys and
sells it is assumed to be charged the same way (to verify against `cashback`).

Not modeled: the v3 synthetic migration (a buy that crosses completion) and
mayhem coins, whose quote reserves follow a different rule. `multi_hop_swap`
hops are only replayed from their recorded amounts (see `_replay_multi_hop`).
"""

from dataclasses import dataclass, replace
from typing import Any, Dict

# Global account values on mainnet (2026-10-15).
INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000
INITIAL_VIRTUAL_QUOTE_RESERVES = 30_000_000_000        # 30 SOL, for SOL-paired coins
INITIAL_REAL_TOKEN_RESERVES    = 793_100_000_000_000
TOKEN_TOTAL_SUPPLY             = 1_000_000_000_000_000
POOL_MIGRATION_FEE             = 15_000_001

BPS = 10_000
ZERO_PUBKEY = "11111111111111111111111111111111"

# Event ix_name -> how the trade was specified.
EXACT_TOKENS = "exact_tokens"
EXACT_QUOTE_IN = "exact_quote_in"
SELL = "sell"
INTENT_BY_IX = {
    "buy": EXACT_TOKENS,
    "buy_v2": EXACT_TOKENS,
    "buy_v3": EXACT_TOKENS,
    "buy_exact_sol_in": EXACT_QUOTE_IN,
    "buy_exact_quote_in": EXACT_QUOTE_IN,
    "buy_exact_quote_in_v2": EXACT_QUOTE_IN,
    "buy_exact_quote_in_v3": EXACT_QUOTE_IN,
    "sell": SELL,
    "sell_v2": SELL,
    "sell_v3": SELL,
}


class CurveError(Exception):
    """The program would reject this trade."""


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


@dataclass(frozen=True)
class FeeRates:
    protocol_bps: int = 95
    creator_bps: int = 30
    cashback_bps: int = 0    # legacy cashback coins: the creator fee goes back to buyers instead

    @property
    def total_bps(self) -> int:
        return self.protocol_bps + self.creator_bps + self.cashback_bps

    def protocol_fee(self, quote: int) -> int:
        return ceil_div(quote * self.protocol_bps, BPS)

    def creator_fee(self, quote: int) -> int:
        return ceil_div(quote * self.creator_bps, BPS)

    def cashback_fee(self, quote: int) -> int:
        return ceil_div(quote * self.cashback_bps, BPS)

    @classmethod
    def for_creator(cls, creator: str, protocol_bps: int = 95, creator_bps: int = 30) -> "FeeRates":
        """Coins whose creator is the zero key (some legacy coins) pay no creator fee."""
        return cls(protocol_bps, 0 if creator == ZERO_PUBKEY else creator_bps)


@dataclass(frozen=True)
class CurveState:
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int = TOKEN_TOTAL_SUPPLY

    @property
    def complete(self) -> bool:
        return self.real_token_reserves == 0

    @property
    def price(self) -> float:
        """Quote per whole token (SOL per token for SOL-paired coins). For display only."""
        return (self.virtual_quote_reserves / 1e9) / (self.virtual_token_reserves / 1e6)

    @property
    def market_cap(self) -> int:
        """In quote base units, as the fee program computes it."""
        return self.virtual_quote_reserves * self.token_total_supply // self.virtual_token_reserves

    @property
    def progress(self) -> float:
        """Share of the curve's sellable tokens already sold, 0..1."""
        return 1 - self.real_token_reserves / INITIAL_REAL_TOKEN_RESERVES

    @classmethod
    def initial(cls) -> "CurveState":
        return cls(INITIAL_VIRTUAL_TOKEN_RESERVES, INITIAL_VIRTUAL_QUOTE_RESERVES,
                   INITIAL_REAL_TOKEN_RESERVES, 0)

    @classmethod
    def from_create_event(cls, e: Dict[str, Any]) -> "CurveState":
        return cls(e["virtual_token_reserves"], e["virtual_quote_reserves"],
                   e["real_token_reserves"], 0, e["token_total_supply"])

    @classmethod
    def after_trade_event(cls, e: Dict[str, Any], token_total_supply: int = TOKEN_TOTAL_SUPPLY) -> "CurveState":
        """The curve state a TradeEvent reports (right after that trade)."""
        return cls(e["virtual_token_reserves"], e["virtual_quote_reserves"],
                   e["real_token_reserves"], e["real_quote_reserves"], token_total_supply)

    def before(self, is_buy: bool, quote_amount: int, token_amount: int) -> "CurveState":
        """Undo a trade: the state right before a trade that left the curve in `self`."""
        q, t = (-quote_amount, token_amount) if is_buy else (quote_amount, -token_amount)
        return replace(self,
                       virtual_quote_reserves=self.virtual_quote_reserves + q,
                       virtual_token_reserves=self.virtual_token_reserves + t,
                       real_quote_reserves=self.real_quote_reserves + q,
                       real_token_reserves=self.real_token_reserves + t)

    def _apply(self, is_buy: bool, quote_amount: int, token_amount: int) -> "CurveState":
        q, t = (quote_amount, -token_amount) if is_buy else (-quote_amount, token_amount)
        return replace(self,
                       virtual_quote_reserves=self.virtual_quote_reserves + q,
                       virtual_token_reserves=self.virtual_token_reserves + t,
                       real_quote_reserves=self.real_quote_reserves + q,
                       real_token_reserves=self.real_token_reserves + t)


@dataclass(frozen=True)
class Fill:
    is_buy: bool
    quote_amount: int      # quote into (buy) or out of (sell) the curve, the event's quote_amount
    token_amount: int
    protocol_fee: int
    creator_fee: int
    state_after: CurveState
    cashback_fee: int = 0

    @property
    def fees(self) -> int:
        return self.protocol_fee + self.creator_fee + self.cashback_fee

    @property
    def trader_quote(self) -> int:
        """Quote the trader pays (buy) or receives (sell), fees included."""
        return self.quote_amount + self.fees if self.is_buy else self.quote_amount - self.fees


def _check_open(state: CurveState):
    if state.complete:
        raise CurveError("BondingCurveComplete")


def buy_exact_tokens(state: CurveState, amount: int, rates: FeeRates) -> Fill:
    """`buy` / `buy_v2` / `buy_v3`: buy exactly `amount` tokens. Slippage is the caller's check."""
    _check_open(state)
    if amount <= 0:
        raise CurveError("amount must be positive")
    if amount > state.real_token_reserves:
        raise CurveError("NotEnoughTokensToBuy")
    cost = amount * state.virtual_quote_reserves // (state.virtual_token_reserves - amount) + 1
    return Fill(True, cost, amount, rates.protocol_fee(cost), rates.creator_fee(cost),
                state._apply(True, cost, amount), rates.cashback_fee(cost))


def _tokens_for_quote(state: CurveState, quote: int) -> int:
    """Tokens the curve gives for `quote` going in (one unit is held back for rounding)."""
    return (quote - 1) * state.virtual_token_reserves // (state.virtual_quote_reserves + quote - 1)


def buy_exact_quote_in(state: CurveState, spendable: int, rates: FeeRates) -> Fill:
    """`buy_exact_sol_in` / `buy_exact_quote_in_v2` / `_v3`: spend at most `spendable`, fees included."""
    _check_open(state)
    x0 = spendable * BPS // (BPS + rates.total_bps)
    protocol_fee, creator_fee, cashback_fee = rates.protocol_fee(x0), rates.creator_fee(x0), rates.cashback_fee(x0)
    quote = x0 - max(0, x0 + protocol_fee + creator_fee + cashback_fee - spendable)
    if quote <= 1:
        raise CurveError("spendable too small")
    tokens = _tokens_for_quote(state, quote)
    if tokens >= state.real_token_reserves:
        return buy_exact_tokens(state, state.real_token_reserves, rates)
    if tokens == 0:
        raise CurveError("buys zero tokens")
    return Fill(True, quote, tokens, protocol_fee, creator_fee, state._apply(True, quote, tokens), cashback_fee)


def sell(state: CurveState, amount: int, rates: FeeRates) -> Fill:
    """`sell` / `sell_v2` / `sell_v3`: sell exactly `amount` tokens."""
    _check_open(state)
    if amount <= 0:
        raise CurveError("amount must be positive")
    gross = amount * state.virtual_quote_reserves // (state.virtual_token_reserves + amount)
    if gross > state.real_quote_reserves:
        raise CurveError("not enough quote in the curve")
    return Fill(False, gross, amount, rates.protocol_fee(gross), rates.creator_fee(gross),
                state._apply(False, gross, amount), rates.cashback_fee(gross))


def replay_trade(state: CurveState, event: Dict[str, Any], rates: FeeRates) -> Fill:
    """
    Re-run a recorded TradeEvent from `state` with this model. The event fixes what
    the trader asked for (token amount, or the total quote spent); everything else
    is computed and can be compared with the event.
    """
    if event["ix_name"] == "multi_hop_swap":
        return _replay_multi_hop(state, event, rates)
    intent = INTENT_BY_IX.get(event["ix_name"])
    if intent == EXACT_TOKENS:
        return buy_exact_tokens(state, event["token_amount"], rates)
    if intent == EXACT_QUOTE_IN and event["real_token_reserves"] == 0:
        # A completing buy refunds what it did not spend, so the event does not tell
        # `spendable`; what it did is buy everything that was left at the exact-tokens cost.
        return buy_exact_tokens(state, event["token_amount"], rates)
    if intent == EXACT_QUOTE_IN:
        spendable = event["quote_amount"] + event["fee"] + event["creator_fee"] + (event.get("cashback") or 0)
        return buy_exact_quote_in(state, spendable, rates)
    if intent == SELL:
        return sell(state, event["token_amount"], rates)
    raise ValueError(f"unknown trade instruction {event['ix_name']!r}")


def _replay_multi_hop(state: CurveState, event: Dict[str, Any], rates: FeeRates) -> Fill:
    """
    A bonding curve hop of `multi_hop_swap`. The hop's input is not in the event,
    so the curve side is replayed from the recorded quote (buy) or token (sell)
    amount. Pass the creator rate the event reports: most hops report 0 (their
    creator fee stays on the curve), a few report the coin's usual 30 bps. The
    protocol fee here is ceil(quote * bps); the program computes it on the hop
    input, so the recorded fee can differ by 1 lamport. A hop that empties the curve buys everything left
    at the exact-tokens cost, like an exact-quote-in buy.
    """
    if not event["is_buy"]:
        return sell(state, event["token_amount"], rates)
    if event["real_token_reserves"] == 0:
        return buy_exact_tokens(state, event["token_amount"], rates)
    quote = event["quote_amount"]
    tokens = _tokens_for_quote(state, quote)
    return Fill(True, quote, tokens, rates.protocol_fee(quote), rates.creator_fee(quote),
                state._apply(True, quote, tokens), rates.cashback_fee(quote))
