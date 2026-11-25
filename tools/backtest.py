"""Local backtest for the pump.fun bot: an approximate replay, NOT the grader.

Streams this kit's frozen dev dataset (dataset/events/*.ndjson.gz) through
the bot's ``on_batch`` as hello / batch / done, one batch per slot that
carries events, and fills orders with a deliberately simple model:

* price of a mint = virtual_quote_reserves / virtual_token_reserves;
* a buy of ``amt`` lamports receives ``amt * (1 - 1.25%) / price`` tokens;
* a sell of ``tok`` tokens receives ``tok * price * (1 - 1.25%)`` lamports;
* every filled order also pays a 105,000-lamport transaction fee
  (5,000 base + 100,000 priority, as the kit's hello says);
* the harness caps are enforced: at most 5 open positions, a batch's buys
  on one mint may spend at most 1% of its real quote reserves, at most 16
  orders per batch;
* a curve's CompleteEvent closes any open position in it at the mark
  price, and whatever is still held at the end is flattened at the last
  mark price.

What this leaves out (so read the numbers as an approximation): the real
harness latency of 2 slots, its exact bonding-curve fill math, and the
difference between the price the bot decides on and the price its fill
lands at. The official check is ``grade-local`` in the kit, which needs
docker or Python 3.13 — neither is on this machine — so this script is
the local stand-in.

Usage:
    python3 tools/backtest.py [--entry 2.5] [--tp 0.25] [--sl -0.20]
        [--hold 7200] [--stake 40000000] [--min-trades 3] [--detail]

The ``--`` overrides patch the bot module's parameters before the run, so
a few variants can be compared without editing the bot. With no overrides
it runs the bot exactly as shipped. Prints one summary line (net PnL in
SOL, buys, round trips of >= 0.01 SOL, fills, max drawdown, top-3 coins'
contribution, PnL without the top 3), and with ``--detail`` also the exit
return distribution.
"""

import argparse
import gzip
import importlib.util
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.dirname(HERE)
KIT_DIR = os.path.expanduser(
    "~/workspace/makefun/kits/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed-kit"
)
SOL = 1_000_000_000
TX_FEE = 105_000            # base 5,000 + priority 100,000 lamports
FEE_RATE = 0.0125           # protocol 95 bps + creator 30 bps
START_CASH = 10 * SOL
MAX_POSITIONS = 5
MAX_ORDERS_PER_BATCH = 16
ROUND_TRIP_MIN = 10_000_000  # 0.01 SOL bought, the competition's bar

HELLO = {
    "type": "hello",
    "protocol": "agent-fun/replay/v2",
    "seed": "42",
    "universe": {
        "program": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "quote": "SOL",
        "mayhem": "excluded",
        "exclude_cashback": True,
        "sample": {"hash": "sha256", "modulus": 16, "residue": 15},
    },
    "capital": {"lamports": str(START_CASH)},
    "caps": {
        "max_open_positions": MAX_POSITIONS,
        "max_order_bps_of_real_reserve": 100,
        "max_orders_per_batch": MAX_ORDERS_PER_BATCH,
        "shorts": False,
        "leverage": False,
    },
    "latency_slots": 2,
    "fees": {
        "protocol_bps_fallback": 95,
        "creator_bps_fallback": 30,
        "base_fee_lamports": "5000",
        "priority_fee_lamports": "100000",
        "failed_tx_bps": 200,
    },
}


def load_bot():
    """Import the bot's main.py as a module (its vendor shim resolves)."""
    sys.path.insert(0, os.path.join(BUILD_DIR, "vendor"))
    spec = importlib.util.spec_from_file_location("pump_bot", os.path.join(BUILD_DIR, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_events():
    """All dev events, grouped by slot in slot order."""
    by_slot = {}
    total = 0
    for part in ("part-00000.ndjson.gz", "part-00001.ndjson.gz", "part-00002.ndjson.gz"):
        path = os.path.join(KIT_DIR, "dataset", "events", part)
        with gzip.open(path, "rt") as handle:
            for line in handle:
                event = json.loads(line)
                by_slot.setdefault(event["slot"], []).append(event)
                total += 1
    return by_slot, total


class Replay:
    """One approximate run of the bot over the dev window."""

    def __init__(self, bot):
        import makefun_bot

        self.bot = bot
        self.state = makefun_bot.BotState(HELLO)
        self.curves = {}          # mint -> [vt, vq, rt, rq, complete]
        self.cash = START_CASH
        self.positions = {}       # mint -> [tokens, cost_lamports]
        self.bought = {}          # mint -> lamports ever spent on buys
        self.realized = {}        # mint -> realized PnL lamports
        self.entry = {}           # mint -> (entry price, entry slot)
        self.exits = []           # (return, hold slots, how) per closed trade
        self.orders_sent = 0
        self.fills = 0
        self.peak_equity = START_CASH
        self.max_drawdown = 0.0
        self.pending_fills = []   # fills reported to the bot on the next batch

    def price(self, mint):
        curve = self.curves.get(mint)
        if not curve or curve[0] <= 0 or curve[1] <= 0:
            return None
        return curve[1] / curve[0]

    def equity(self):
        total = self.cash
        for mint, (tokens, _cost) in self.positions.items():
            price = self.price(mint)
            if price is not None:
                total += tokens * price
        return total

    def close_position(self, mint, how, slot):
        """Sell a whole position at the mark price (complete / flatten)."""
        tokens, cost = self.positions.pop(mint)
        price = self.price(mint)
        if price is None:
            return
        proceeds = int(tokens * price * (1 - FEE_RATE)) - TX_FEE
        self.cash += proceeds
        self.realized[mint] = self.realized.get(mint, 0) + proceeds - cost
        self.fills += 1
        if self.bought.get(mint, 0) >= ROUND_TRIP_MIN:
            self.round_trips.add(mint)
        entry_price, entry_slot = self.entry.pop(mint, (0, slot))
        if entry_price > 0:
            self.exits.append((price / entry_price - 1.0, slot - entry_slot, how))

    def run(self, by_slot):
        import makefun_bot  # noqa: F401  (already imported in __init__)

        self.round_trips = set()
        seq = 0
        for slot in sorted(by_slot):
            events = by_slot[slot]
            touched = []
            for event in events:
                mint = event["mint"]
                curve = self.curves.setdefault(mint, [0, 0, 0, 0, False])
                if event["event"] == "CreateEvent":
                    curve[0] = int(event["virtual_token_reserves"])
                    curve[1] = int(event["virtual_quote_reserves"])
                    curve[2] = int(event["real_token_reserves"])
                    curve[3] = 0
                elif event["event"] == "TradeEvent":
                    curve[0] = int(event["virtual_token_reserves"])
                    curve[1] = int(event["virtual_quote_reserves"])
                    curve[2] = int(event["real_token_reserves"])
                    curve[3] = int(event["real_quote_reserves"])
                else:  # CompleteEvent
                    curve[4] = True
                    if mint in self.positions:
                        self.close_position(mint, "complete", slot)
                touched.append(mint)
            marks = [
                {
                    "mint": mint,
                    "slot": slot,
                    "virtual_token_reserves": str(self.curves[mint][0]),
                    "virtual_quote_reserves": str(self.curves[mint][1]),
                    "real_token_reserves": str(self.curves[mint][2]),
                    "real_quote_reserves": str(self.curves[mint][3]),
                }
                for mint in dict.fromkeys(touched)
            ]
            account = {
                "cash_lamports": str(self.cash),
                "positions": [
                    {"mint": mint, "token_amount": str(tokens), "cost_lamports": str(cost)}
                    for mint, (tokens, cost) in self.positions.items()
                ],
            }
            batch = {
                "type": "batch",
                "seq": seq,
                "slot": slot,
                "events": events,
                "marks": marks,
                "fills": self.pending_fills,
                "account": account,
            }
            self.pending_fills = []
            self.state.apply_batch(batch)
            orders = self.state.to_orders(self.bot.on_batch(self.state, batch))
            self.state.record_orders(orders)
            seq += 1
            self.fill_orders(orders, slot)
            equity = self.equity()
            if equity > self.peak_equity:
                self.peak_equity = equity
            drawdown = (self.peak_equity - equity) / self.peak_equity if self.peak_equity else 0.0
            self.max_drawdown = max(self.max_drawdown, drawdown)
        for mint in list(self.positions):
            self.close_position(mint, "flatten", slot)
        return self.summarize()

    def fill_orders(self, orders, slot):
        """Fill the bot's orders at the current mark price, caps enforced."""
        for order in orders[:MAX_ORDERS_PER_BATCH]:
            self.orders_sent += 1
            mint = order["mint"]
            curve = self.curves.get(mint)
            if curve is None or curve[4]:
                continue
            if order["side"] == "buy":
                cap = curve[3] * 100 // 10_000
                amount = min(int(order["quote_lamports"]), cap)
                if amount <= 0 or self.cash < amount + TX_FEE:
                    continue
                if mint not in self.positions and len(self.positions) >= MAX_POSITIONS:
                    continue
                price = self.price(mint)
                if price is None or price <= 0:
                    continue
                tokens = int(amount * (1 - FEE_RATE) / price)
                if tokens <= 0:
                    continue
                self.cash -= amount + TX_FEE
                held, cost = self.positions.get(mint, (0, 0))
                self.positions[mint] = (held + tokens, cost + amount)
                self.bought[mint] = self.bought.get(mint, 0) + amount
                self.entry[mint] = (amount / tokens, slot)
                self.fills += 1
                self.pending_fills.append(
                    {
                        "client_id": order["client_id"],
                        "mint": mint,
                        "side": "buy",
                        "status": "filled",
                        "reason": None,
                        "decided_slot": slot,
                        "slot": slot + 1,
                        "quote_lamports": str(amount),
                        "token_amount": str(tokens),
                        "fee_lamports": str(int(amount * FEE_RATE)),
                        "tx_fee_lamports": str(TX_FEE),
                        "fee_fallback": False,
                    }
                )
            else:  # sell
                held, cost = self.positions.get(mint, (0, 0))
                tokens = min(int(order["token_amount"]), held)
                if tokens <= 0:
                    continue
                price = self.price(mint)
                if price is None:
                    continue
                proceeds = int(tokens * price * (1 - FEE_RATE)) - TX_FEE
                self.cash += proceeds
                share = tokens / held if held else 0
                self.realized[mint] = self.realized.get(mint, 0) + proceeds - cost * share
                self.fills += 1
                if self.bought.get(mint, 0) >= ROUND_TRIP_MIN:
                    self.round_trips.add(mint)
                entry_price, entry_slot = self.entry.get(mint, (0, slot))
                if entry_price > 0:
                    self.exits.append((price / entry_price - 1.0, slot - entry_slot, "sell"))
                if tokens >= held:
                    self.positions.pop(mint, None)
                    self.entry.pop(mint, None)
                else:
                    self.positions[mint] = (held - tokens, cost * (held - tokens) // held)
                self.pending_fills.append(
                    {
                        "client_id": order["client_id"],
                        "mint": mint,
                        "side": "sell",
                        "status": "filled",
                        "reason": None,
                        "decided_slot": slot,
                        "slot": slot + 1,
                        "quote_lamports": str(proceeds),
                        "token_amount": str(tokens),
                        "fee_lamports": str(int(tokens * price * FEE_RATE)),
                        "tx_fee_lamports": str(TX_FEE),
                        "fee_fallback": False,
                    }
                )

    def summarize(self):
        pnl = self.cash - START_CASH
        top3 = sum(sorted(self.realized.values(), reverse=True)[:3])
        return {
            "pnl_sol": pnl / SOL,
            "buys": len(self.bought),
            "round_trips": len(self.round_trips),
            "orders": self.orders_sent,
            "fills": self.fills,
            "max_drawdown": self.max_drawdown,
            "top3_sol": top3 / SOL,
            "pnl_ex_top3_sol": (pnl - top3) / SOL,
            "exit_returns": [ret for ret, _hold, _how in self.exits],
            "exit_holds": [hold for _ret, hold, _how in self.exits],
        }


def main():
    parser = argparse.ArgumentParser(description="Approximate local backtest for the pump bot.")
    parser.add_argument("--entry", type=float, default=None)
    parser.add_argument("--tp", type=float, default=None)
    parser.add_argument("--sl", type=float, default=None)
    parser.add_argument("--hold", type=int, default=None)
    parser.add_argument("--stake", type=int, default=None)
    parser.add_argument("--min-trades", type=int, default=None)
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()

    bot = load_bot()
    overrides = {
        "ENTRY_MULT": args.entry,
        "TAKE_PROFIT": args.tp,
        "STOP_LOSS": args.sl,
        "MAX_HOLD_SLOTS": args.hold,
        "STAKE_LAMPORTS": args.stake,
        "MIN_TRADES": args.min_trades,
    }
    applied = {k: v for k, v in overrides.items() if v is not None}
    for key, value in applied.items():
        setattr(bot, key, value)
    if applied:
        print(f"overrides: {applied}")

    by_slot, total = load_events()
    print(f"dataset: {total} events, {len(by_slot)} slots, {len({e['mint'] for evs in by_slot.values() for e in evs})} mints")
    summary = Replay(bot).run(by_slot)
    print(
        f"RESULT pnl={summary['pnl_sol']:+.4f} SOL buys={summary['buys']} "
        f"round_trips>={summary['round_trips']} orders={summary['orders']} "
        f"fills={summary['fills']} maxDD={summary['max_drawdown']:.3f} "
        f"top3={summary['top3_sol']:+.4f} pnl_ex_top3={summary['pnl_ex_top3_sol']:+.4f}"
    )
    if args.detail and summary["exit_returns"]:
        rets = summary["exit_returns"]
        print(
            f"  exits n={len(rets)} mean={statistics.mean(rets):+.3f} "
            f"median={statistics.median(rets):+.3f} "
            f"share>=+40%: {sum(1 for r in rets if r >= 0.40) / len(rets):.2f} "
            f"share<=-20%: {sum(1 for r in rets if r <= -0.20) / len(rets):.2f} "
            f"median hold={statistics.median(summary['exit_holds']):.0f} slots"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
