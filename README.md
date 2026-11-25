[![Competition](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/monicazhy949/makefun-pump-bot-01/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![Reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/monicazhy949/makefun-pump-bot-01/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

# makefun-pump-bot-01

Paper trading bot for the [Pump.fun Bot Sprint: Make Bot And Win](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) competition. It trades 10 SOL of paper money on a replay of recorded pump.fun bonding-curve activity (protocol `agent-fun/replay/v2`). Nothing is traded for real.

## Strategy

Confirmed early runner with staged exits — "buy the launch only after it proves it can run, sell the move, never marry a coin":

- **Buy:** a coin becomes a candidate on its `CreateEvent`. It is bought once, on the first batch where the harness size cap allows at least 0.005 SOL, it has printed at least 3 trades, and its price is at least 2.5x its creation price — only proven runners, never the launch itself. Never above 3.5x creation (no chasing parabolic launches), never below 0.6x (dead launch). Flat 0.04 SOL per coin, at most 5 open positions, and only when free cash covers the buy plus tx fees. A coin skipped for a transient reason (no free slot, no free cash) stays a candidate; a coin that trips a permanent filter is dropped for good.
- **Sell (whole position):** take profit at +25%, stop loss at −20%, or a time exit after 7,200 slots (~48 minutes of chain time at ~0.4 s per slot) whatever the position did. No trailing stop: the first version had one and the local sweep showed it cost money on the dev window, so it was removed. The replay closes completed curves and flattens the rest at the end.
- **Determinism:** no clock, no environment, no randomness; candidate order follows the batch's own event/mark order. Two runs on the same input produce identical action logs (verified).

## Backtest (dev window, honestly reported)

Local approximate replay of the kit's frozen dev dataset (128,058 events, 63,369 slots, 1,222 mints) via `tools/backtest.py` — simplified fills (price = virtual quote / virtual token reserves, 1.25% fee + 105,000 lamports per order, harness caps enforced, no latency). This is an approximation, not the official grader (`grade-local` needs docker or Python 3.13, neither available on this machine).

| variant | net PnL | buys / round trips ≥ 0.01 SOL | max drawdown |
|---|---|---|---|
| entry 1.8x, TP +40% / SL −20%, trailing, hold 5,400 | −0.5107 SOL | 118 / 118 | 0.058 |
| entry 2.0x, TP +40% / SL −20%, trailing, hold 5,400 | −0.2764 SOL | 90 / 90 | 0.038 |
| entry 2.5x, TP +40% / SL −20%, trailing, hold 5,400 | −0.1403 SOL | 53 / 53 | 0.019 |
| entry 3.0x, TP +40% / SL −20%, trailing, hold 5,400 | −0.1799 SOL | 40 / 40 | 0.021 |
| entry 2.5x, TP +25% / SL −15%, trailing, hold 7,200 | −0.1214 SOL | 53 / 53 | 0.016 |
| entry 2.5x, TP +25% / SL −15%, no trailing, hold 7,200 | −0.0975 SOL | 53 / 53 | 0.012 |
| **entry 2.5x, TP +25% / SL −20%, no trailing, hold 7,200 (SHIPPED)** | **−0.0714 SOL** | **53 / 53** | **0.011** |

Shipped result: **net PnL −0.0714 SOL (negative, honestly reported), 53 buys, 53 round trips ≥ 0.01 SOL (the bar is 20), 106 orders / 106 fills, max drawdown 0.011, top-3 coins +0.0511 SOL, PnL without the top 3 −0.1225 SOL**. Exit mix: mean exit return −1.6%, median −21.3% (the stop loss does most of the selling — 53% of exits are at −20% or worse), median hold 131 slots. Every variant tried lost money on this one dev window; the shipped parameters lost least and had the lowest drawdown. The final window is different data, so this is not a prediction.

## Layout

- `main.py` — the entrypoint (Python 3.13, standard library only).
- `vendor/makefun_bot.py` — the kit's Python SDK shim, byte-identical copy of `sdk/python/makefun_bot.py`.
- `tools/backtest.py` — the local approximate replay used for the table above.
- `tools/backtest_result.txt` — the shipped run's output.

## Run

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot reads `hello` / `batch` / `done` JSON lines on stdin and writes one `reply` per batch on stdout; diagnostics go to stderr.

```sh
python3 tools/backtest.py --detail   # re-run the local backtest
```

## License

MIT — see [LICENSE](LICENSE).
