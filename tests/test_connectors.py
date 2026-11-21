"""Unit tests for Vanta data connectors (Phase 2).

All HTTP is mocked (unittest.mock) — no live network in unit tests.
Sample responses live in data/fixtures/ (hand-made, labelled fixtures).
Covers: parsing per connector, failure handling (HTTP 500 -> [] and no
invented value), proxy labelling (implied lease, Yahoo quotes),
FedWatch/Finnhub without key -> failed, and the registry refresh.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests  # noqa: E402

from goldboard_data import (  # noqa: E402
    COMEXConnector, COTConnector, ETFConnector, FedWatchConnector,
    FinnhubConnector, FREDConnector, GDELTConnector, LBMAConnector,
    MicrostructureConnector, PriceConnector, SupplyConnector, WGCConnector,
    get_connector, list_connectors, refresh_all,
)
from goldboard_data.base import ConnectorError  # noqa: E402
from goldboard_data.fedwatch import next_meetings  # noqa: E402
from goldboard_data.wgc import IMF_HOLDINGS_TONNES  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "..", "data", "fixtures")


def read(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def read_bytes(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as fh:
        return fh.read()


class FakeResp:
    def __init__(self, text="", status=200, json_data=None, content=None):
        self.text = text
        self.status_code = status
        self._json = json_data
        self.content = content if content is not None else text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


def resp_500(*args, **kwargs):
    return FakeResp("server error", status=500)


def by_id(snaps, series_id):
    for s in snaps:
        if s.series_id == series_id:
            return s
    raise AssertionError(f"missing snapshot {series_id}")


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.sleep_patch = mock.patch("time.sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        GDELTConnector._last_call = 0.0


# ---------------------------------------------------------------- COT
class TestCOT(BaseTest):
    def test_legacy_parse(self):
        snaps = COTConnector().parse_legacy(read("cot_sample.txt"))
        self.assertEqual(by_id(snaps, "cot_gold_oi").value, 407834)
        self.assertEqual(by_id(snaps, "cot_gold_nc_net").value, 166132)
        self.assertAlmostEqual(by_id(snaps, "cot_gold_nc_net_pct_oi").value,
                               166132 / 407834 * 100, places=2)
        self.assertEqual(by_id(snaps, "cot_gold_commercial_net").value, -193750)
        self.assertEqual(by_id(snaps, "cot_gold_nc_net").as_of.year, 2026)
        self.assertEqual(by_id(snaps, "cot_gold_nc_net").as_of.month, 11)
        self.assertEqual(by_id(snaps, "cot_gold_nc_net").as_of.day, 17)
        self.assertTrue(all(s.source_url for s in snaps))

    def test_disaggregated_parse(self):
        snaps = COTConnector().parse_disaggregated(read("cot_disagg_sample.txt"))
        self.assertEqual(by_id(snaps, "cot_gold_mm_net").value, 102942)
        self.assertAlmostEqual(by_id(snaps, "cot_gold_mm_net_pct_oi").value,
                               102942 / 407834 * 100, places=2)
        self.assertEqual(by_id(snaps, "cot_gold_swap_net").value, -189159)
        self.assertEqual(by_id(snaps, "cot_gold_producer_net").value, -4591)

    def test_missing_gold_code_fails_loudly(self):
        with self.assertRaises(ConnectorError):
            COTConnector().parse_legacy(
                '"SILVER - COMMODITY EXCHANGE INC.",261117,2026-11-17,088692,'
                'CMX ,01,088 ,  123456,   50000,   40000,    5000,   30000,'
                '   35000,   85000,   75000,   38456,   48456\n')

    def test_too_few_fields_fails_loudly(self):
        with self.assertRaises(ConnectorError):
            COTConnector().parse_legacy(
                '"GOLD - COMMODITY EXCHANGE INC.",261117,2026-11-17,088691,'
                'CMX ,01,088 ,  407834,  210051,   43919\n')

    def test_fetch_http500_returns_empty_not_invented(self):
        conn = COTConnector()
        with mock.patch("requests.get", side_effect=resp_500):
            snaps = conn.fetch()
        self.assertEqual(snaps, [])
        self.assertIsNotNone(conn.last_error)

    def test_fetch_mocked(self):
        def get(url, **kw):
            if "deafut" in url:
                return FakeResp(read("cot_sample.txt"))
            return FakeResp(read("cot_disagg_sample.txt"))
        with mock.patch("requests.get", side_effect=get):
            snaps = COTConnector().fetch()
        self.assertEqual(by_id(snaps, "cot_gold_mm_net").value, 102942)
        self.assertEqual(by_id(snaps, "cot_gold_nc_net").value, 166132)


# ---------------------------------------------------------------- FRED
class TestFRED(BaseTest):
    def test_parse_api_json(self):
        snaps = FREDConnector().parse_api_json(
            json.loads(read("fred_sample.json")), "DFII10")
        s = by_id(snaps, "fred_dfii10")
        self.assertEqual(s.value, 2.62)
        self.assertEqual(s.vintage, "2026-11-20")
        self.assertEqual(s.unit, "%")

    def test_parse_csv(self):
        snaps = FREDConnector().parse_csv(read("fred_sample.csv"), "DFII10")
        self.assertEqual(by_id(snaps, "fred_dfii10").value, 2.62)

    def test_csv_missing_values_skipped(self):
        text = "DATE,DFII10\n2026-11-19,2.61\n2026-11-20,.\n"
        snaps = FREDConnector().parse_csv(text, "DFII10")
        self.assertEqual(by_id(snaps, "fred_dfii10").value, 2.61)

    def test_fetch_series_keyless_uses_fredgraph_csv(self):
        seen = {}
        def get(url, params=None, **kw):
            seen["url"] = url
            seen["params"] = params
            return FakeResp(read("fred_sample.csv"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOLD_BOARD_FRED_KEY", None)
            with mock.patch("requests.get", side_effect=get):
                snaps = FREDConnector().fetch_series("DFII10")
        self.assertIn("fredgraph.csv", seen["url"])
        self.assertEqual(seen["params"]["id"], "DFII10")
        self.assertEqual(by_id(snaps, "fred_dfii10").value, 2.62)

    def test_fetch_series_with_key_uses_api(self):
        def get(url, params=None, **kw):
            assert "api.stlouisfed.org" in url
            return FakeResp(json_data=json.loads(read("fred_sample.json")))
        with mock.patch.dict(os.environ, {"GOLD_BOARD_FRED_KEY": "KEY123"}):
            with mock.patch("requests.get", side_effect=get):
                snaps = FREDConnector().fetch_series("DFII10")
        self.assertEqual(by_id(snaps, "fred_dfii10").value, 2.62)

    def test_fetch_macro_all_fail_returns_empty(self):
        with mock.patch("requests.get", side_effect=resp_500):
            self.assertEqual(FREDConnector().fetch_macro(), [])


# ---------------------------------------------------------------- WGC
class TestWGC(BaseTest):
    def test_parse_wgc_csv(self):
        snaps = WGCConnector().parse_wgc_csv_file(
            os.path.join(FIX, "wgc_sample.csv"))
        self.assertEqual(len(snaps), 4)
        self.assertEqual(by_id(snaps, "wgc_cb_net_tonnes_poland").value, 15.2)
        self.assertEqual(by_id(snaps, "wgc_cb_net_tonnes_china").value, 23.0)
        self.assertEqual(by_id(snaps, "wgc_cb_net_tonnes_turkey").value, -5.0)

    def test_imf_holdings_constant(self):
        self.assertEqual(IMF_HOLDINGS_TONNES, 2814.1)

    def test_fetch_returns_imf_reference_when_scrape_fails(self):
        with mock.patch("requests.get", side_effect=resp_500):
            snaps = WGCConnector().fetch()
        self.assertEqual(by_id(snaps, "imf_holdings_tonnes").value, 2814.1)

    def test_parse_page_with_net_purchases(self):
        html = ("<p>Central banks made net purchases of 61 tonnes in "
                "September, led by Poland and China.</p>")
        snaps = WGCConnector().parse(html)
        self.assertEqual(by_id(snaps, "wgc_cb_net_tonnes").value, 61.0)

    def test_parse_page_without_figure_fails(self):
        with self.assertRaises(ConnectorError):
            WGCConnector().parse("<p>Gold is shiny.</p>")


# ---------------------------------------------------------------- ETF
class TestETF(BaseTest):
    def test_parse_gld_csv(self):
        snaps = ETFConnector().parse_gld_csv(os.path.join(FIX, "etf_sample.csv"))
        self.assertEqual(by_id(snaps, "etf_gld_tonnes").value, 1034.43)
        self.assertAlmostEqual(
            by_id(snaps, "etf_gld_daily_change_tonnes").value, -3.71, places=2)

    def test_parse_gld_xlsx(self):
        from goldboard_data.etf import parse_gld_xlsx
        rows = parse_gld_xlsx(read_bytes("etf_sample.xlsx"))
        self.assertEqual(rows[-1]["tonnes"], 1034.43)
        snaps = ETFConnector()._snapshots_from_holdings("gld", rows)
        self.assertAlmostEqual(
            by_id(snaps, "etf_gld_daily_change_tonnes").value, -3.71, places=2)

    def test_parse_gld_xlsx_garbage_fails(self):
        from goldboard_data.etf import parse_gld_xlsx
        with self.assertRaises(ConnectorError):
            parse_gld_xlsx(b"not a zip file at all")

    def test_parse_gld_page(self):
        snaps = ETFConnector().parse_gld_page(read("gld_page_sample.html"))
        self.assertEqual(by_id(snaps, "etf_gld_tonnes").value, 1034.434)
        self.assertEqual(by_id(snaps, "etf_gld_ounces").value, 33258084.55)
        self.assertEqual(by_id(snaps, "etf_gld_tonnes").as_of.day, 20)

    def test_parse_gld_page_without_tonnes_fails(self):
        with self.assertRaises(ConnectorError):
            ETFConnector().parse_gld_page("<html><body>no holdings</body></html>")

    def test_fetch_iau_page(self):
        # Must match "Tonnes in Trust 460.11", never the year 2026.
        with mock.patch("requests.get",
                        return_value=FakeResp(read("iau_page_sample.html"))):
            snaps = ETFConnector().fetch_iau()
        self.assertEqual(by_id(snaps, "etf_iau_tonnes").value, 460.11)
        self.assertEqual(by_id(snaps, "etf_iau_tonnes").as_of.day, 20)

    def test_fetch_all_fail_returns_empty(self):
        with mock.patch("requests.get", side_effect=resp_500):
            self.assertEqual(ETFConnector().fetch(), [])


# ---------------------------------------------------------------- COMEX
class TestCOMEX(BaseTest):
    def test_parse_html(self):
        snaps = COMEXConnector().parse(read("comex_sample.html"))
        self.assertEqual(by_id(snaps, "comex_registered_oz").value, 14200000)
        # Eligible is derived as total - registered, labelled as proxy.
        self.assertEqual(by_id(snaps, "comex_eligible_oz").value, 8660000)
        self.assertTrue(by_id(snaps, "comex_eligible_oz").is_proxy)
        self.assertEqual(by_id(snaps, "comex_total_oz").value, 22860000)
        self.assertAlmostEqual(
            by_id(snaps, "comex_registered_share_pct").value,
            14200000 / 22860000 * 100, places=2)
        self.assertEqual(by_id(snaps, "comex_registered_oz").as_of.day, 20)

    def test_parse_csv(self):
        snaps = COMEXConnector().parse_comex_csv(
            os.path.join(FIX, "comex_sample.csv"))
        self.assertEqual(by_id(snaps, "comex_registered_oz").value, 14200000)

    def test_parse_garbage_fails(self):
        with self.assertRaises(ConnectorError):
            COMEXConnector().parse("<html><body>nothing here</body></html>")


# ---------------------------------------------------------------- LBMA
class TestLBMA(BaseTest):
    def test_parse_membership(self):
        snaps = LBMAConnector().parse(read("lbma_sample.html"))
        # 11 banks only: quoting minimums + nav items must be filtered.
        self.assertEqual(by_id(snaps, "lbma_market_maker_count").value, 11)
        self.assertEqual(
            by_id(snaps, "lbma_market_maker_jp_morgan_chase_bank").value, 1.0)
        self.assertEqual(
            by_id(snaps, "lbma_market_maker_citibank_n_a").value, 1.0)
        ids = [s.series_id for s in snaps]
        self.assertFalse(any("spot" in i or "membership" in i for i in ids))

    def test_parse_membership_garbage_fails(self):
        with self.assertRaises(ConnectorError):
            LBMAConnector().parse("<html><body>no makers here</body></html>")

    def test_fetch_vault(self):
        with mock.patch("requests.get",
                        return_value=FakeResp(read("lbma_vault_sample.html"))):
            snaps = LBMAConnector().fetch_vault()
        self.assertEqual(by_id(snaps, "lbma_vault_gold_tonnes").value, 9850)

    def test_fetch_clearing(self):
        with mock.patch("requests.get",
                        return_value=FakeResp(read("lbma_clearing_sample.html"))):
            snaps = LBMAConnector().fetch_clearing()
        self.assertEqual(by_id(snaps, "lbma_clearing_gold_oz").value, 18300000)
        self.assertEqual(by_id(snaps, "lbma_clearing_transfers").value, 7438)
        self.assertEqual(by_id(snaps, "lbma_clearing_gold_oz").as_of.month, 9)
        self.assertEqual(by_id(snaps, "lbma_clearing_gold_oz").as_of.year, 2026)


# ---------------------------------------------------------------- FedWatch
class TestFedWatch(BaseTest):
    def test_no_key_fails(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConnectorError) as ctx:
                FedWatchConnector().fetch()
        self.assertIn("$25/mo", str(ctx.exception))

    def test_key_but_no_endpoint_fails(self):
        with mock.patch.dict(os.environ,
                             {"GOLD_BOARD_FEDWATCH_KEY": "K"}, clear=True):
            with self.assertRaises(ConnectorError):
                FedWatchConnector().fetch()

    def test_parse_with_key_mocked(self):
        with mock.patch.dict(os.environ,
                             {"GOLD_BOARD_FEDWATCH_KEY": "K",
                              "GOLD_BOARD_FEDWATCH_URL": "https://api.example.com/fw"},
                             clear=True):
            with mock.patch("requests.get", return_value=FakeResp(
                    json_data=json.loads(read("fedwatch_sample.json")))):
                snaps = FedWatchConnector().fetch()
        self.assertEqual(
            by_id(snaps, "fedwatch_prob_2026_12_09_cut_25bp").value, 62.5)

    def test_next_meetings_from_calendar(self):
        meetings = next_meetings()
        self.assertTrue(any(m["start"] == "2026-12-08" for m in meetings))
        self.assertTrue(any(m.get("sep") for m in meetings))


# ---------------------------------------------------------------- GDELT
class TestGDELT(BaseTest):
    def test_parse_volume(self):
        c = GDELTConnector()
        c._mode_hint = "timelinevol"
        snaps = c.parse(json.loads(read("gdelt_vol_sample.json")))
        self.assertEqual(by_id(snaps, "gdelt_gold_mentions_latest").value, 1450)
        self.assertAlmostEqual(
            by_id(snaps, "gdelt_gold_mentions_7d_avg").value,
            sum([1100, 1250, 1180, 1400, 1320, 1500, 1450]) / 7, places=2)

    def test_parse_tone(self):
        c = GDELTConnector()
        c._mode_hint = "timelinetone"
        snaps = c.parse(json.loads(read("gdelt_tone_sample.json")))
        self.assertEqual(by_id(snaps, "gdelt_gold_tone_latest").value, 1.5)

    def test_empty_timeline_fails(self):
        with self.assertRaises(ConnectorError):
            GDELTConnector().parse({"timeline": []})

    def test_fetch_mocked(self):
        calls = {"n": 0}
        def get(url, params=None, **kw):
            calls["n"] += 1
            if params and params.get("mode") == "timelinetone":
                return FakeResp(json_data=json.loads(read("gdelt_tone_sample.json")))
            return FakeResp(json_data=json.loads(read("gdelt_vol_sample.json")))
        with mock.patch("requests.get", side_effect=get):
            snaps = GDELTConnector().fetch()
        self.assertEqual(by_id(snaps, "gdelt_gold_mentions_latest").value, 1450)
        self.assertEqual(by_id(snaps, "gdelt_gold_tone_latest").value, 1.5)


# ---------------------------------------------------------------- Finnhub
class TestFinnhub(BaseTest):
    def test_no_key_fails(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConnectorError) as ctx:
                FinnhubConnector().fetch()
        self.assertIn("key required", str(ctx.exception).lower())

    def test_parse_news_filters_gold(self):
        snaps = FinnhubConnector().parse_news(
            json.loads(read("finnhub_news_sample.json")))
        self.assertEqual(by_id(snaps, "finnhub_gold_news_count").value, 2)

    def test_parse_sentiment(self):
        snaps = FinnhubConnector().parse_sentiment(
            json.loads(read("finnhub_sentiment_sample.json")))
        self.assertAlmostEqual(
            by_id(snaps, "finnhub_gld_bullish_pct").value, 58)

    def test_fetch_mocked(self):
        def get(url, params=None, **kw):
            if "news-sentiment" in url:
                return FakeResp(json_data=json.loads(
                    read("finnhub_sentiment_sample.json")))
            return FakeResp(json_data=json.loads(read("finnhub_news_sample.json")))
        with mock.patch.dict(os.environ, {"GOLD_BOARD_FINNHUB_KEY": "K"}):
            with mock.patch("requests.get", side_effect=get):
                snaps = FinnhubConnector().fetch()
        self.assertEqual(by_id(snaps, "finnhub_gold_news_count").value, 2)
        self.assertAlmostEqual(
            by_id(snaps, "finnhub_gld_bullish_pct").value, 58)


# ---------------------------------------------------------------- Supply
class TestSupply(BaseTest):
    def test_parse_supply_csv(self):
        snaps = SupplyConnector().parse_supply_csv(
            os.path.join(FIX, "supply_sample.csv"))
        self.assertEqual(by_id(snaps, "supply_mine_production_tonnes").value, 1071)
        self.assertEqual(by_id(snaps, "supply_central_banks_tonnes").value, 321)
        aisc = by_id(snaps, "supply_aisc_usd_per_oz")
        self.assertEqual(aisc.value, 1930)
        self.assertEqual(aisc.unit, "USD/oz")

    def test_parse_usgs_text(self):
        snaps = SupplyConnector().parse(
            "<p>World mine production was 3,300 t in 2025.</p>")
        self.assertEqual(
            by_id(snaps, "supply_usgs_world_mine_production_tonnes").value, 3300)

    def test_parse_usgs_garbage_fails(self):
        with self.assertRaises(ConnectorError):
            SupplyConnector().parse("<p>gold is nice</p>")


# ---------------------------------------------------------------- Microstructure
class TestMicrostructure(BaseTest):
    def test_carry_and_implied_lease_proxy(self):
        c = MicrostructureConnector()
        with mock.patch.object(MicrostructureConnector, "_yahoo_price",
                               return_value=(4075.5, None)), \
             mock.patch.object(MicrostructureConnector, "_far_contract",
                               return_value=("GCZ27.CMX", 4273.0, 365)), \
             mock.patch.object(MicrostructureConnector, "_sofr",
                               return_value=4.12):
            snaps = c.fetch_carry()
        carry = by_id(snaps, "futures_carry_12m_pct")
        self.assertAlmostEqual(carry.value, (4273.0 / 4075.5 - 1) * 100, places=2)
        self.assertTrue(carry.is_proxy)
        lease = by_id(snaps, "implied_lease_proxy_pct")
        self.assertTrue(lease.is_proxy)
        self.assertEqual(lease.proxy_formula,
                         "implied_lease = SOFR - futures_carry_12m")
        self.assertAlmostEqual(lease.value, 4.12 - carry.value, places=2)

    def test_far_contract_falls_back_to_next_month(self):
        # Dec contract 404s, Aug contract resolves: carry uses Aug.
        c = MicrostructureConnector()
        def yahoo(symbol):
            if symbol == "GCZ27.CMX":
                raise ConnectorError("404")
            if symbol == "GCQ27.CMX":
                return (3600.0, None)
            return (3500.0, None)
        with mock.patch.object(MicrostructureConnector, "_yahoo_price",
                               side_effect=yahoo):
            symbol, price, days = c._far_contract()
        self.assertEqual(symbol, "GCQ27.CMX")
        self.assertEqual(price, 3600.0)
        self.assertGreater(days, 200)

    def test_far_contract_all_fail_raises(self):
        c = MicrostructureConnector()
        with mock.patch.object(MicrostructureConnector, "_yahoo_price",
                               side_effect=ConnectorError("404")):
            with self.assertRaises(ConnectorError):
                c._far_contract()

    def test_gvz_labelled_proxy(self):
        c = MicrostructureConnector()
        with mock.patch.object(MicrostructureConnector, "_yahoo_price",
                               return_value=(28.0, None)):
            snaps = c.fetch_gvz()
        s = by_id(snaps, "gvz")
        self.assertEqual(s.value, 28.0)
        self.assertTrue(s.is_proxy)

    def test_options_oi_unparseable_fails(self):
        with mock.patch("requests.get",
                        return_value=FakeResp("<html>js app</html>")):
            with self.assertRaises(ConnectorError):
                MicrostructureConnector().fetch_options_oi()

    def test_parse_chart_json(self):
        snaps = MicrostructureConnector().parse(
            json.loads(read("price_sample.json")))
        self.assertEqual(by_id(snaps, "futures_front_price").value, 4075.5)


# ---------------------------------------------------------------- Price
class TestPrice(BaseTest):
    def test_parse_chart_json(self):
        snaps = PriceConnector().parse(json.loads(read("price_sample.json")))
        s = by_id(snaps, "price_xauusd")
        self.assertEqual(s.value, 4075.5)
        self.assertTrue(s.is_proxy)
        self.assertIn("Yahoo", s.proxy_formula)

    def test_fetch_http500_returns_empty(self):
        with mock.patch("requests.get", side_effect=resp_500):
            self.assertEqual(PriceConnector().fetch(), [])

    def test_fetch_mocked(self):
        with mock.patch("requests.get", return_value=FakeResp(
                json_data=json.loads(read("price_sample.json")))):
            snaps = PriceConnector().fetch()
        self.assertEqual(by_id(snaps, "price_xauusd").value, 4075.5)


# ---------------------------------------------------------------- Registry
class TestRegistry(BaseTest):
    def test_list_connectors(self):
        names = list_connectors()
        for expected in ("cot", "fred", "wgc", "etf", "comex", "lbma",
                         "fedwatch", "gdelt", "finnhub", "supply",
                         "microstructure", "price"):
            self.assertIn(expected, names)
        self.assertEqual(len(names), 12)

    def test_get_connector(self):
        self.assertIsInstance(get_connector("cot"), COTConnector)
        with self.assertRaises(KeyError):
            get_connector("nope")

    def test_refresh_all_never_raises_on_failure(self):
        with mock.patch("requests.get", side_effect=resp_500), \
             mock.patch.dict(os.environ, {}, clear=True):
            report = refresh_all()
        self.assertEqual(set(report), set(list_connectors()))
        # Every live source fails; wgc still returns its static IMF
        # reference snapshot (a holding, not a flow) by design.
        failed = {n for n, r in report.items() if r["status"] == "failed"}
        self.assertEqual(failed, set(list_connectors()) - {"wgc"})
        self.assertEqual(report["wgc"]["status"], "ok")
        self.assertEqual(report["wgc"]["snapshots"], 1)

    def test_refresh_all_marks_ok_source(self):
        def get(url, params=None, **kw):
            if "cftc.gov" in url:
                if "deafut" in url:
                    return FakeResp(read("cot_sample.txt"))
                return FakeResp(read("cot_disagg_sample.txt"))
            return FakeResp("err", status=500)
        with mock.patch("requests.get", side_effect=get), \
             mock.patch.dict(os.environ, {}, clear=True):
            report = refresh_all()
        self.assertEqual(report["cot"]["status"], "ok")
        self.assertGreater(report["cot"]["snapshots"], 0)
        self.assertEqual(report["price"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
