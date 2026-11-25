# MCX India Option Chain Fair-Value & Buy-Side Alert Bot (Upstox API)

A GitHub-ready, read-only Python scanner that builds live option chains for **all optionable MCX India commodity contracts** using the **Upstox Developer API only**, computes theoretical fair value using **Black-76 (options on futures)** + **Put-Call Parity**, and sends instant **BUY-SIDE ONLY** alerts when an option trades significantly below fair value.

> **Important Upstox API Architecture Note:** Upstox's `/v2/option/chain` endpoint currently states that it is [not available for the MCX exchange](https://upstox.com/developer/api-documentation/get-pc-option-chain/). Therefore, this bot builds live MCX option chains dynamically using Upstox's official MCX endpoints:
> 1. **BOD Instruments Master** (`MCX.json.gz`, [Upstox Instruments Docs](https://upstox.com/developer/api-documentation/instruments/)) — auto-discovers every active MCX Futures and Options contract (`CRUDEOIL`, `CRUDEOILM`, `NATURALGAS`, `NATGASMINI`, `GOLD`, `GOLDM`, `SILVER`, `SILVERM`, `COPPER`, `ZINC`, etc.).
> 2. **Option Greeks V3 API** (`/v3/market-quote/option-greek`, up to 50 keys/call, [Upstox Option Greeks Docs](https://upstox.com/developer/api-documentation/option-greek/)) — live IV, Delta, Gamma, Theta, Vega, OI, Volume, LTP.
> 3. **Full Market Quotes V2 API** (`/v2/market-quote/quotes`, up to 500 keys/call, [Upstox Full Market Quotes Docs](https://upstox.com/developer/api-documentation/get-full-market-quote/)) — live top-5 Bid/Ask depth so every buy alert clears the actual executable **Ask** price.

## How the Pricing Model Works

1. **Old-School Commodity Formula — Black-76 (Options on Futures):**
   MCX commodity options settle/devolve into underlying futures contracts, not spot prices ([Zerodha Varsity: Commodity Options & Black-76](https://zerodha.com/varsity/chapter/commodity-options/), [OpenAlgo: Black-Scholes vs Black-76](https://openalgo.in/quant/options-pricing)). For futures price $F$, strike $K$, time to expiry $T$ (years), risk-free rate $r$, and volatility $\sigma$:
   - $C = e^{-rT}[F \cdot N(d_1) - K \cdot N(d_2)]$
   - $P = e^{-rT}[K \cdot N(-d_2) - F \cdot N(-d_1)]$
2. **Non-Circular Peer IV (OI & Spread Weighted):**
   Plugging an option's own IV back into Black-76 simply returns its own LTP. Instead, the bot computes a **leave-one-out peer IV** interpolated from liquid neighbouring strikes weighted by $\sqrt{\text{OI}} / \text{Spread}$.
3. **Put-Call Parity Cross-Check:**
   Checks model-free futures parity $C - P = e^{-rT}(F - K)$ against the opposite contract at the same strike and takes the conservative fair value (`min(Black-76, Parity)`) when both are available.
4. **Automatic 4–5 Day Expiry Rollover:**
   Fetches the nearest active expiry for each MCX commodity. When `<= 5` calendar days remain (`ROLLOVER_DTE=5`, configurable to `4` or `5`), it automatically switches to the **next month's expiry** and matches the corresponding underlying futures contract.
5. **Strict BUY-Side Only Filter:**
   Only alerts when `Actual Fair Value > LTP` **and** `Actual Fair Value > Ask Price`, and only after deducting estimated MCX round-trip charges (Upstox brokerage ₹20×2, 0.05% CTT on sell premium, 0.0418% MCX exchange fee on both legs, 0.003% stamp duty, SEBI fee, and 18% GST).

## Minimum Mispricing Gap % (Forum & Blog Research Summary)

Why **1–2% is usually too tight** for retail MCX option buyers:
- **MCX Statutory + Brokerage Costs:** 0.05% CTT on sell premium ([Share India CTT Guide](https://www.shareindia.com/knowledge-center/regulatory-compliance-updates/commodity-transaction-tax-ctt)), 0.0418% MCX exchange transaction fee on both legs ([OneTradeJournal MCX Calculator](https://onetradejournal.com/tools/commodity-trading-cost-calculator-mcx)), 0.003% buy stamp duty ([Navia F&O Charges](https://navia.co.in/blog/zero-brokerage-on-fo-charges-you-still-pay/)), 18% GST, and ₹20/order brokerage ([Upstox Brokerage Charges](https://upstox.com/brokerage-charges/)).
- **Bid-Ask Spreads & Slippage:** While market makers quoting inside the spread start flagging **2% IV residuals** ([FlashAlpha Volatility Surface Guide](https://flashalpha.com/articles/volatility-surface-api-how-to-build-visualize-trade-iv-surface)), retail MCX buyers crossing the ask face 1–4%+ spreads and thinner order books outside Crude Oil/Natural Gas ([Reddit r/IndianStreetBets MCX Discussion](https://www.reddit.com/r/IndianStreetBets/comments/192iyjb/how_to_start_trading_commodities/)).
- **Recommended Thresholds (built into this bot):**
  - `5% – 10%` (**STANDARD**): Practical minimum floor for liquid ATM Crude Oil / Natural Gas options (`MIN_GAP_PCT=5.0`).
  - `10% – 15%` (**STRONG**): Recommended for Gold, Silver, Copper, and Zinc options with wider spreads.
  - `15%+` (**HIGH_CONVICTION**): Strongest mispricing tier.

## Quick Start

```bash
pip install -r requirements.txt

# 1. Run unit tests
python -m unittest discover -s tests -v

# 2. Run offline Crude Oil 7500 demo (no API token needed)
python mcx_option_bot.py --mode demo

# 3. Test live Upstox + Telegram connectivity
export UPSTOX_ACCESS_TOKEN="your_token"
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python mcx_option_bot.py --mode test

# 4. Run live scanner
python mcx_option_bot.py --mode run
```

## GitHub Actions Setup
Upload the extracted files to a GitHub repository, add `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` under **Settings → Secrets and variables → Actions**, and run the **MCX Option Fair-Value Buy Alerts** workflow in `test` mode first.
