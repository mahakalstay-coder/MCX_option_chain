import math
import unittest
from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

import mcx_option_bot as bot

IST = ZoneInfo("Asia/Kolkata")


class TestMCXOptionFairValueModel(unittest.TestCase):
    def test_black76_put_call_parity_identity(self):
        """Black-76 CE - PE must equal exp(-rT) * (F - K)."""
        f, k, t, vol, r = 7550.0, 7500.0, 30 / 365.0, 0.42, 0.065
        ce = bot.black76_price(f, k, t, vol, "CE", r)
        pe = bot.black76_price(f, k, t, vol, "PE", r)
        expected_diff = math.exp(-r * t) * (f - k)
        self.assertAlmostEqual(ce - pe, expected_diff, places=6)

    def test_expiry_rollover_4_to_5_days(self):
        """When <= 5 days remain, automatically roll over to next month's expiry."""
        today = date(2026, 11, 25)
        expiries = [date(2026, 11, 29), date(2026, 12, 17), date(2027, 1, 19)]

        # 4 days left <= 5 -> must roll over to Dec 17
        selected, rolled, dte = bot.select_target_expiry(expiries, today, rollover_dte=5)
        self.assertEqual(selected, date(2026, 12, 17))
        self.assertTrue(rolled)
        self.assertEqual(dte, 22)

        # 5 days left <= 5 -> must roll over
        expiries_5d = [date(2026, 11, 30), date(2026, 12, 17)]
        selected2, rolled2, _ = bot.select_target_expiry(expiries_5d, today, rollover_dte=5)
        self.assertEqual(selected2, date(2026, 12, 17))
        self.assertTrue(rolled2)

        # 10 days left > 5 -> keep current expiry
        expiries_10d = [date(2026, 12, 5), date(2026, 12, 17)]
        selected3, rolled3, _ = bot.select_target_expiry(expiries_10d, today, rollover_dte=5)
        self.assertEqual(selected3, date(2026, 12, 5))
        self.assertFalse(rolled3)

    def test_crude_7500_buy_side_alert_and_ignore_overpriced(self):
        """Crude at 7500: underpriced 7500PE (~50 Rs gap) triggers BUY alert; overpriced CE is ignored."""
        now = datetime(2026, 11, 25, 18, 0, tzinfo=IST)
        exp = date(2026, 12, 17)
        t_years = (datetime.combine(exp, dt_time(23, 30), tzinfo=IST) - now).total_seconds() / (365 * 86400)
        f = 7500.0
        sigma = 0.75

        def make_opt(strike, side, ltp, bid, ask, iv_pct):
            return bot.OptionContract(
                symbol="CRUDEOIL",
                trading_symbol=f"CRUDEOIL {strike} {side} 17 DEC 26",
                instrument_key=f"MCX_FO|{strike}_{side}",
                underlying_key="MCX_FO|FUT",
                side=side,
                strike=float(strike),
                expiry=exp,
                lot_size=100,
                tick_size=0.1,
                futures_price=f,
                ltp=ltp,
                bid=bid,
                ask=ask,
                oi=1500,
                volume=500,
                iv=iv_pct,
            )

        p7400_pe = bot.black76_price(f, 7400, t_years, sigma, "PE")
        p7600_pe = bot.black76_price(f, 7600, t_years, sigma, "PE")
        p7400_ce = bot.black76_price(f, 7400, t_years, sigma, "CE")
        p7600_ce = bot.black76_price(f, 7600, t_years, sigma, "CE")
        fair_7500 = bot.black76_price(f, 7500, t_years, sigma, "PE")

        chain = [
            make_opt(7400, "PE", p7400_pe, p7400_pe - 1, p7400_pe + 1, sigma * 100),
            make_opt(7600, "PE", p7600_pe, p7600_pe - 1, p7600_pe + 1, sigma * 100),
            make_opt(7400, "CE", p7400_ce, p7400_ce - 1, p7400_ce + 1, sigma * 100),
            make_opt(7600, "CE", p7600_ce, p7600_ce - 1, p7600_ce + 1, sigma * 100),
            # Overpriced 7500CE (LTP = fair + 60) -> MUST NOT trigger any alert (BUY side only)
            make_opt(7500, "CE", fair_7500 + 60, fair_7500 + 59, fair_7500 + 61, 83.0),
            # Underpriced 7500PE (LTP = fair - 50, Ask = fair - 48) -> MUST trigger BUY alert!
            make_opt(7500, "PE", fair_7500 - 50, fair_7500 - 52, fair_7500 - 48, 68.0),
        ]

        alerts = bot.evaluate_chain_buy_opportunities(chain, now, rolled_over=True)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.side, "PE")
        self.assertEqual(alert.strike, 7500.0)
        self.assertGreaterEqual(alert.gap_rs_ltp, 49.0)
        self.assertGreaterEqual(alert.gap_rs_ask, 47.0)
        self.assertGreater(alert.net_edge_rs, 40.0)
        self.assertTrue(alert.rolled_over)

    def test_wide_ask_blocks_false_buy_alert(self):
        """If LTP looks cheap but executable Ask is at fair value, no BUY alert fires."""
        now = datetime(2026, 11, 25, 18, 0, tzinfo=IST)
        exp = date(2026, 12, 17)
        t_years = (datetime.combine(exp, dt_time(23, 30), tzinfo=IST) - now).total_seconds() / (365 * 86400)
        f, sigma = 7500.0, 0.50
        fair = bot.black76_price(f, 7500, t_years, sigma, "PE")
        p7400 = bot.black76_price(f, 7400, t_years, sigma, "PE")
        p7600 = bot.black76_price(f, 7600, t_years, sigma, "PE")

        chain = [
            bot.OptionContract("CRUDEOIL", "C1", "K1", "UF", "PE", 7400, exp, 100, 0.1, f, p7400, p7400 - 1, p7400 + 1, 1000, 500, 50.0),
            bot.OptionContract("CRUDEOIL", "C2", "K2", "UF", "PE", 7600, exp, 100, 0.1, f, p7600, p7600 - 1, p7600 + 1, 1000, 500, 50.0),
            # Stale LTP is 40 Rs below fair, but Ask is right at fair value
            bot.OptionContract("CRUDEOIL", "C3", "K3", "UF", "PE", 7500, exp, 100, 0.1, f, fair - 40, fair - 4, fair, 1000, 500, 44.0),
        ]
        alerts = bot.evaluate_chain_buy_opportunities(chain, now)
        self.assertEqual(len(alerts), 0)

    def test_group_mcx_universe_filters_optionable_only(self):
        """Only MCX commodities with BOTH FUT and CE/PE options are included."""
        sample_master = [
            {"segment": "MCX_FO", "instrument_type": "FUT", "underlying_symbol": "CRUDEOIL", "instrument_key": "MCX_FO|1"},
            {"segment": "MCX_FO", "instrument_type": "CE", "underlying_symbol": "CRUDEOIL", "instrument_key": "MCX_FO|2"},
            {"segment": "MCX_FO", "instrument_type": "FUT", "underlying_symbol": "MENTHAOIL", "instrument_key": "MCX_FO|3"},
        ]
        u = bot.group_mcx_universe(sample_master, symbol_filter="ALL")
        self.assertIn("CRUDEOIL", u)
        self.assertNotIn("MENTHAOIL", u)


if __name__ == "__main__":
    unittest.main()
