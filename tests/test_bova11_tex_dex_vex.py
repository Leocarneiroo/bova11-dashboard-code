import math
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import bova11_tex_dex_vex as mod


def _manual_charm_tau(spot, strike, sigma, t, rate=0.0):
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    norm_pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return norm_pdf * ((2.0 * rate * t) - (d2 * sigma * root_t)) / (2.0 * t * sigma * root_t)


def _manual_delta(spot, strike, sigma, t, option_type, rate=0.0):
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * root_t)
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return nd1 if option_type == "call" else nd1 - 1.0


def _manual_gamma(spot, strike, sigma, t, rate=0.0):
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * root_t)
    norm_pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return norm_pdf / (spot * sigma * root_t)


def _manual_merton(spot, strike, sigma, t, rate=0.0, q=0.0):
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate - q + 0.5 * sigma * sigma) * t) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    norm_pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    eq = math.exp(-q * t)

    delta_call = eq * nd1
    delta_put = eq * (nd1 - 1.0)
    gamma = eq * norm_pdf / (spot * sigma * root_t)

    front = -(spot * eq * norm_pdf * sigma) / (2.0 * root_t)
    theta_call = (front - q * spot * eq * nd1 - rate * strike * math.exp(-rate * t) * (0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0))))) / 365.0
    charm_base = norm_pdf * ((2.0 * (rate - q) * t) - (d2 * sigma * root_t)) / (2.0 * t * sigma * root_t)
    charm_call_tau = eq * (-q * nd1 + charm_base)

    return {
        "delta_call": delta_call,
        "delta_put": delta_put,
        "gamma": gamma,
        "theta_call_per_day": theta_call,
        "charm_call_tau": charm_call_tau,
    }


class TexDexVexCharmTests(unittest.TestCase):
    def test_resolve_pricing_context_derives_forward_and_q_from_bid_ask(self):
        rows = [
            {
                "strike": 100.0,
                "c_oi": 60000.0,
                "p_oi": 40000.0,
                "c_bid": 2.10,
                "c_ask": 2.20,
                "p_bid": 1.80,
                "p_ask": 1.90,
            },
        ]

        ctx = mod.resolve_pricing_context(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_ref=100.0,
            rate=0.10,
        )

        expected_t = mod._resolve_session_and_expiry_t("8 mai W2", "29abr")
        expected_forward = 100.0 + (2.15 - 1.85) * math.exp(0.10 * expected_t)
        expected_q = 0.10 - (math.log(expected_forward / 100.0) / expected_t)

        self.assertAlmostEqual(ctx["t_years"], expected_t, places=12)
        self.assertAlmostEqual(ctx["forward"], expected_forward, places=12)
        self.assertAlmostEqual(ctx["q"], expected_q, places=12)

    def test_build_unified_spot_profile_uses_merton_forward_and_q(self):
        rows = [
            {
                "strike": 100.0,
                "c_oi": 60000.0,
                "p_oi": 40000.0,
                "c_iv": 20.0,
                "p_iv": 20.0,
                "c_bid": 2.10,
                "c_ask": 2.20,
                "p_bid": 1.80,
                "p_ask": 1.90,
            },
        ]

        profile = mod.build_unified_spot_profile(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_range=[100.0],
            spot_ref=100.0,
            rate=0.10,
        )

        ctx = mod.resolve_pricing_context(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_ref=100.0,
            rate=0.10,
        )
        merton = _manual_merton(100.0, 100.0, 0.20, ctx["t_years"], rate=0.10, q=ctx["q"])

        row = profile[0]
        gex_factor = (100.0 ** 2) / 100.0

        self.assertAlmostEqual(row["gex_call"], merton["gamma"] * 60000.0 * gex_factor, places=12)
        self.assertAlmostEqual(row["dex_retail_call"], merton["delta_call"] * 60000.0, places=12)
        self.assertAlmostEqual(row["dex_retail_put"], merton["delta_put"] * 40000.0, places=12)
        self.assertAlmostEqual(row["tex_call"], (-merton["theta_call_per_day"]) * 60000.0 * 100.0, places=10)
        self.assertAlmostEqual(row["cex_call"], (merton["charm_call_tau"] / 252.0) * 60000.0, places=10)

    def test_build_dex_profiles_match_retail_and_fm_formulas(self):
        rows = [
            {"strike": 100.0, "c_oi": 1200.0, "p_oi": 800.0, "c_iv": 22.0, "p_iv": 24.0},
        ]

        retail_profile, fm_profile = mod.build_dex_profiles(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_range=[100.0],
            rate=0.0,
        )

        t_years = mod._resolve_session_and_expiry_t("8 mai W2", "29abr")
        call_delta = _manual_delta(100.0, 100.0, 0.22, t_years, "call")
        put_delta = _manual_delta(100.0, 100.0, 0.24, t_years, "put")

        self.assertAlmostEqual(retail_profile[0]["dex_call"], call_delta * 1200.0, places=12)
        self.assertAlmostEqual(retail_profile[0]["dex_put"], put_delta * 800.0, places=12)
        self.assertAlmostEqual(retail_profile[0]["dex_net"], (call_delta * 1200.0) + (put_delta * 800.0), places=12)

        self.assertAlmostEqual(fm_profile[0]["dex_call"], -(call_delta * 1200.0), places=12)
        self.assertAlmostEqual(fm_profile[0]["dex_put"], -(put_delta * 800.0), places=12)
        self.assertAlmostEqual(fm_profile[0]["dex_net"], -((call_delta * 1200.0) + (put_delta * 800.0)), places=12)

    def test_build_gex_profile_matches_gamma_formula(self):
        rows = [
            {"strike": 100.0, "c_oi": 1200.0, "p_oi": 800.0, "c_iv": 22.0, "p_iv": 24.0},
        ]

        profile = mod.build_gex_profile(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_range=[100.0],
            rate=0.0,
        )

        t_years = mod._resolve_session_and_expiry_t("8 mai W2", "29abr")
        call_gamma = _manual_gamma(100.0, 100.0, 0.22, t_years)
        put_gamma = _manual_gamma(100.0, 100.0, 0.24, t_years)
        factor = (100.0 ** 2) / 100.0

        expected_call = call_gamma * 1200.0 * factor
        expected_put = -put_gamma * 800.0 * factor

        self.assertAlmostEqual(profile[0]["gex_call"], expected_call, places=12)
        self.assertAlmostEqual(profile[0]["gex_put"], expected_put, places=12)
        self.assertAlmostEqual(profile[0]["gex_net"], expected_call + expected_put, places=12)

    def test_build_cex_profile_matches_tau_charm_formula(self):
        rows = [
            {"strike": 100.0, "c_oi": 1200.0, "p_oi": 800.0, "c_iv": 22.0, "p_iv": 24.0},
        ]

        profile = mod.build_cex_profile(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_range=[100.0],
            rate=0.0,
        )

        self.assertEqual(len(profile), 1)
        row = profile[0]
        t_years = mod._resolve_session_and_expiry_t("8 mai W2", "29abr")

        expected_call = _manual_charm_tau(100.0, 100.0, 0.22, t_years) * 1200.0 / 252.0
        expected_put = _manual_charm_tau(100.0, 100.0, 0.24, t_years) * 800.0 / 252.0

        self.assertAlmostEqual(row["cex_call"], expected_call, places=12)
        self.assertAlmostEqual(row["cex_put"], expected_put, places=12)
        self.assertAlmostEqual(row["cex_net"], expected_call + expected_put, places=12)

    def test_build_cex_profile_returns_zero_when_iv_is_missing(self):
        rows = [
            {"strike": 100.0, "c_oi": 1200.0, "p_oi": 800.0, "c_iv": None, "p_iv": None},
        ]

        profile = mod.build_cex_profile(
            rows=rows,
            venc_name="8 mai W2",
            tag_d="29abr",
            spot_range=[100.0, 101.0],
            rate=0.0,
        )

        self.assertEqual(
            profile,
            [
                {"spot": 100.0, "cex_call": 0.0, "cex_put": 0.0, "cex_net": 0.0},
                {"spot": 101.0, "cex_call": 0.0, "cex_put": 0.0, "cex_net": 0.0},
            ],
        )


if __name__ == "__main__":
    unittest.main()
