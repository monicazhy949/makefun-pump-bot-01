"""Pump.fun Bot Sprint: Make Bot And Win — paper trading bot.

Protocol ``agent-fun/replay/v2`` (Python 3.13, standard library only). The
bot reads ``hello`` / ``batch`` / ``done`` JSON lines on stdin and writes one
``reply`` per batch on stdout; diagnostics go to stderr. It trades paper
money only: 10 SOL starting capital, no leverage, no shorts, nothing real.

Strategy — "confirmed early runner, staged exit":

* A coin becomes a candidate when its ``CreateEvent`` arrives. Its creation
  price is the quote-per-token ratio of the creation reserves.
* Buy once, on the first batch where all of these hold: the harness size cap
  for the coin allows at least 0.005 SOL, the coin has printed at least 3
  trades (proof somebody is actually trading it, not just a stillborn
  launch), and its price is at least 2.5x its creation price (only proven
  runners are bought, never the launch itself) but no more than 3.5x (never
  chase a launch that already went parabolic) and no less than 0.6x (a
  launch that already dumped is dead). The stake is a flat 0.04 SOL
  (``min(0.04 SOL, cap)``), at most 5 open positions, and only when free
  cash covers the buy plus the transaction fee. A coin skipped because no
  slot or no cash was free stays a candidate and is reconsidered on a
  later batch; a coin that trips a permanent filter (chase / dead / curve
  completed) is dropped for good.
* Sell the whole position, checked every batch, on whichever comes first:
  take profit at +25% over the entry price, stop loss at -20% under it, or
  a time exit: a position still held 7,200 slots after entry (about 48
  minutes of chain time at ~0.4 s per slot) is sold whatever it did, because most coins go
  quiet minutes after launch and a dead coin must not sit in one of the 5
  slots forever. There is deliberately no trailing stop: the first version
  of this bot had one (arm at +15%, sell 20% below the peak) and the local
  sweep showed it cost money on the dev window — it sold winners into
  noise that then recovered — so it was removed. The replay itself closes
  a completed curve and flattens whatever is left at the end, so no
  end-of-run logic is needed here.

Why these numbers: on this kit's dev window, buying every launch loses
money, and buying only coins that already ran loses least; the wider -20%
stop (instead of -15%) lets a runner breathe through the first dip, which
is where most of the dev-window losses came from. The numbers were picked
from a small local sweep (see tools/backtest.py); the dev window is one
small sample and the final window is different data, so none of this is a
promise. The shipped parameters still lose money on the dev window
(-0.0714 SOL, honestly reported in the README) — every variant tried did.

Determinism: no clock, no environment, no randomness. Candidate order
follows the batch's own event order, then its mark order; open positions
are iterated in the order the account reports them. Two runs on the same
input therefore produce identical action logs, as the grader requires.

The SDK shim is vendored at vendor/makefun_bot.py (a byte-identical copy
of sdk/python/makefun_bot.py from the kit, as the kit instructs).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- Tunable strategy parameters (one place, plain numbers) -----------------
STAKE_LAMPORTS = 40_000_000          # 0.04 SOL per coin, flat sizing
MIN_BUY_LAMPORTS = 5_000_000        # 0.005 SOL: smallest buy worth placing
MAX_SLIPPAGE_BPS = 10_000           # slippage left wide open
ENTRY_MULT = 2.5                    # buy once the price is >= 2.5x creation
CHASE_MULT = 3.5                    # above this the launch already ran away
DEAD_MULT = 0.6                     # below this the launch already dumped
MIN_TRADES = 3                      # trades seen before a coin may be bought
TAKE_PROFIT = 0.25                  # sell at +25% over entry
STOP_LOSS = -0.20                   # sell at -20% under entry
MAX_HOLD_SLOTS = 7_200              # time exit, in slots


class OpenBook:
    """What the bot remembers about one open (or about-to-open) position."""

    __slots__ = ("entry_price", "entry_slot")

    def __init__(self, entry_price, entry_slot):
        self.entry_price = entry_price
        self.entry_slot = entry_slot


# mint -> creation price (quote lamports per token), in first-seen order.
# A mint stays here while it is still a candidate; decided coins are removed.
candidates = {}
# mint -> OpenBook for every position the bot opened (and has not sold yet).
books = {}


def quote_price(state, mint):
    """Current price of a mint in quote lamports per token, or None."""
    curve = state.curves.get(mint)
    if curve is None:
        return None
    if curve.virtual_token_reserves <= 0 or curve.virtual_quote_reserves <= 0:
        return None
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


def ordered_mints(batch):
    """Mints touched by this batch, events first then marks, each once."""
    seen = {}
    for event in batch["events"]:
        seen[event["mint"]] = True
    for mark in batch["marks"]:
        seen[mark["mint"]] = True
    return list(seen)


def decide_sells(state, batch):
    """Whole-position exits, checked for every holding this batch."""
    orders = []
    for mint, holding in list(state.positions.items()):
        book = books.get(mint)
        if book is None:
            # A position the bot did not open (should not happen): leave it
            # to the replay's own closes instead of guessing an exit.
            continue
        price = quote_price(state, mint)
        if price is None:
            continue
        ret = price / book.entry_price - 1.0
        reason = None
        if ret >= TAKE_PROFIT:
            reason = "take-profit"
        elif ret <= STOP_LOSS:
            reason = "stop-loss"
        elif batch["slot"] - book.entry_slot >= MAX_HOLD_SLOTS:
            reason = "time-exit"
        if reason is None:
            continue
        orders.append(
            {
                "side": "sell",
                "mint": mint,
                "token_amount": holding.token_amount,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        )
        print(f"sell {mint[:8]} {reason} ret={ret:+.2%} slot={batch['slot']}", file=sys.stderr)
        del books[mint]
    return orders


def decide_buys(state, batch, orders, free_cash, free_slots):
    """One first buy per candidate coin, in batch order."""
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    for mint in ordered_mints(batch):
        if len(orders) >= caps["max_orders_per_batch"]:
            break
        if mint in candidates:
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                del candidates[mint]
                continue
            price = quote_price(state, mint)
            if price is None:
                continue
            creation = candidates[mint]
            mult = price / creation
            if mult > CHASE_MULT or mult < DEAD_MULT:
                # Parabolic chase or dead launch: dropped for good.
                del candidates[mint]
                continue
            if curve.trades < MIN_TRADES or mult < ENTRY_MULT:
                # Not confirmed yet: stays a candidate for a later batch.
                continue
            cap = state.max_buy_lamports(mint)
            if cap < MIN_BUY_LAMPORTS:
                continue
            amount = min(cap, STAKE_LAMPORTS)
            if free_slots <= 0 or free_cash < amount + tx:
                # Transient skip (no slot / no cash): reconsider later.
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": amount,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            )
            books[mint] = OpenBook(price, batch["slot"])
            print(f"buy {mint[:8]} mult={mult:.2f}x amount={amount} slot={batch['slot']}", file=sys.stderr)
            del candidates[mint]
            free_cash -= amount + tx
            free_slots -= 1
    return orders


def on_batch(state, batch):
    # New launches become candidates, with their creation price.
    for event in batch["events"]:
        if event["event"] == "CreateEvent" and event["mint"] not in candidates:
            curve = state.curves.get(event["mint"])
            if curve is not None and curve.virtual_token_reserves > 0:
                candidates[event["mint"]] = (
                    curve.virtual_quote_reserves / curve.virtual_token_reserves
                )
    orders = decide_sells(state, batch)
    free_cash = state.free_cash_lamports()
    free_slots = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
    return decide_buys(state, batch, orders, free_cash, free_slots)


if __name__ == "__main__":
    makefun_bot.run(on_batch)
