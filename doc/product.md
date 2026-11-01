# Crow Trading: Product Requirements

## Vision
Crow Trading is a **personal quantitative research and paper-trading tool** for testing short-term swing-trading hypotheses on cryptocurrency and stocks. The intended user is a solo Python developer in Denmark, experimenting with positions of roughly **€50** and willing to wait hours or days for a valid setup.

**The goal is to find out whether any tested approach has a repeatable, meaningful edge after fees, spreads, slippage, and relevant tax considerations. Profit is not guaranteed.**

## Target users and use cases
- Research and compare crypto pairs (initially BTC/EUR and ETH/EUR) and selected liquid stocks on the same analytical basis.
- Download and cache historical OHLCV data with documented provenance and limitations.
- Backtest simple long-only swing strategies over multiple market regimes and timeframes.
- Compare each experiment against a suitable buy-and-hold or cash benchmark.
- Simulate entries and exits at approximately €50 per position without spending real funds.
- Review trades, net results, maximum drawdown, win/loss distribution, costs, and parameter sensitivity.
- Export a complete trade ledger to assist with independent Danish tax reporting.

## Core product requirements (MVP)
1. **Market data:** crypto through CCXT/exchange sources; stock research data through yfinance initially. Flag restrictions on Kraken historical candles and yfinance intraday history. Store UTC timestamps, original source metadata, and adjusted/unadjusted pricing status.
2. **Unified strategy interface:** identical strategy logic accepts market-agnostic candle input; signal calculations must use only information known at the time.
3. **Two baseline strategies:** a momentum/breakout variant and a mean-reversion/pullback variant, both explicitly specified before evaluation. No parameter mining disguised as validation.
4. **Backtesting:** fixed initial capital, cash accounting, configurable €50 notional target, whole/fractional quantity constraints, commissions, spread, slippage, and realistic execution timing.
5. **Evaluation:** full trade ledger; total/annualized returns when meaningful, benchmark comparison, max drawdown, exposure, turnover, profit factor, and sample counts; data splits for train/validation/untouched out-of-sample tests.
6. **Paper trading:** same strategy signal and risk-check interfaces with simulated execution; no external trade credentials required.
7. **Safeguards:** live trading disabled, leverage/shorting disabled, explicit data freshness checks, configurable order/exposure/loss caps.
8. **Tax awareness:** preserve trade-level quantities, prices, transaction fees, asset identifiers, timestamps, and EUR/DKK conversion data. Provide only separately labeled, assumptions-based after-tax estimates.

## Not in MVP
- Live broker orders, automated deposits/withdrawals, leverage, derivatives, social/copy trading, high-frequency trading, AI-driven price predictions, portfolio optimization, hosted infrastructure, or guarantees of quick cash.
- Filing tax returns or asserting definitive asset-specific Danish tax treatment.

## Success criteria
- Reproducibly download, validate, cache, and reload at least one crypto and one stock daily OHLCV dataset.
- Run the same strategy implementation on both asset classes without using broker code in the strategy.
- Produce a fully reconcilable €50-scale trade report including all modeled costs and a benchmark.
- Include automated tests for no look-ahead, closed candles, position caps, empty data, non-trading days, and fee calculations.
- Demonstrate documented training/test separation; positive results are **not** a requirement for project success.

## Decisions to revisit before live trading
Broker/account eligibility in Denmark, permitted automated-trading API use, fractional shares, minimum order size, total loss budget, exact fees, supported symbols, tax treatment, and ongoing operational requirements.

See [architecture.md](architecture.md) and [implementation.md](implementation.md).
