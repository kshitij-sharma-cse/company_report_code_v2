#!/usr/bin/env python3
"""Credit model from public records: default curve, loan marks, scorecard, Merton, expected loss, stress tests, migration, projections, Monte Carlo,
recovery, CECL, debt capacity and pricing. Reads the CSVs written by model_financials.py and the data folder; assumptions come from the config.
The rating is a model output, not a rating from any agency.

    python model_credit.py --data DIR --fin OUT/model --config model_config/databricks.json --out OUT/model
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
from statistics import NormalDist

import numpy as np

N = NormalDist()
RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C"]
NOTCH = {r: i + 1 for i, r in enumerate(RATINGS)}


def read(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(x):
    try:
        return float(str(x).replace(",", "")) if x not in ("", None) else None
    except ValueError:
        return None


def letter(n: float) -> str:
    return RATINGS[min(max(int(n + 0.5), 1), len(RATINGS)) - 1]


def on_or_before(series: dict, d: dt.date):
    k = max(x for x in series if x <= d.isoformat())
    return k, series[k]


def run(data: Path, fin: Path, cfg: dict, out: Path) -> dict:
    C, as_of = cfg["credit"], dt.date.fromisoformat(cfg["as_of"])
    D = data / "data"
    out.mkdir(parents=True, exist_ok=True)

    def write(name, header, body):
        with open(out / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(body)

    # ---------------------------------------------------------------- default curve
    sp = read(D / "sp_nrsro_corporate_default_rates.csv")
    tm_rows = read(D / "sp_nrsro_transition_matrices.csv")
    fit_rows = [r for r in sp if int(r["notch"]) <= C["fit_max_notch"]]
    x_ = np.array([float(r["notch"]) for r in fit_rows])
    n_ = np.array([float(r["n_outstanding"]) for r in fit_rows])
    d_ = np.array([round(float(r["default_rate"]) * float(r["n_outstanding"])) for r in fit_rows], float)
    T_ = np.array([float(r["horizon_years"]) for r in fit_rows])
    X = np.column_stack([np.ones(len(x_)), x_])
    beta = np.array([-10.0, 0.5])

    def ll(b):
        p = np.clip(1 - np.exp(-T_ * np.exp(X @ b)), 1e-300, 1 - 1e-16)
        return float(np.sum(d_ * np.log(p) + (n_ - d_) * np.log(1 - p)))

    for _ in range(200):
        mu = T_ * np.exp(X @ beta)
        p = np.clip(1 - np.exp(-mu), 1e-300, 1 - 1e-16)
        dp = mu * (1 - p)
        w = n_ * dp ** 2 / (p * (1 - p))
        z = (X @ beta) + (d_ - n_ * p) / np.maximum(n_ * dp, 1e-300)
        new = np.linalg.solve(X.T @ (w[:, None] * X), X.T @ (w * z))
        while ll(new) < ll(beta) - 1e-9:
            new = (new + beta) / 2
        done = np.max(np.abs(new - beta)) < 1e-12
        beta = new
        if done:
            break
    hazard = lambda notch: math.exp(beta[0] + beta[1] * notch)                                     # noqa: E731
    pd_at = lambda notch, h: 1 - math.exp(-h * hazard(notch))                                      # noqa: E731
    obs = lambda h, rt: next((r["default_rate"] for r in sp if int(r["horizon_years"]) == h and r["rating"] == rt), "")      # noqa: E731
    write("credit_pd_curve_fitted.csv", ["rating", "notch", "annual_hazard", "pd_1y_fitted", "pd_3y_fitted", "pd_5y_fitted", "pd_10y_fitted",
                                         "pd_1y_observed_2025", "pd_3y_observed_2022_25", "pd_10y_observed_2015_25"],
          [[rt, NOTCH[rt], round(hazard(NOTCH[rt]), 6)] + [round(pd_at(NOTCH[rt], h), 6) for h in (1, 3, 5, 10)] + [obs(h, rt) for h in (1, 3, 10)] for rt in RATINGS[:20]])
    # ---------------------------------------------------------------- rates
    with open(data / "sources" / "nyfed" / "nyfed_sofr_2024-12-01_2026-10-31.json", encoding="utf-8") as fh:
        SOFR = {x["effectiveDate"]: x["percentRate"] / 100 for x in json.load(fh)["refRates"]}
    TSY: dict = {}
    for p in sorted((data / "sources" / "treasury").glob("treasury_par_yield_curve_*.csv")):
        for r in read(p):
            TSY[dt.datetime.strptime(r["Date"], "%m/%d/%Y").date().isoformat()] = {k: float(r[k]) / 100 for k in ("1 Yr", "3 Yr", "5 Yr", "10 Yr") if r.get(k)}
    sofr = lambda d: on_or_before(SOFR, d)[1]                                                       # noqa: E731
    cmt = lambda t: on_or_before(TSY, as_of)[1][t]                                                  # noqa: E731
    basis_date = dt.date.fromisoformat(C["basis_date"])
    write("credit_rates_used.csv", ["series", "as_of_requested", "observation_date", "rate"],
          [["SOFR", d, on_or_before(SOFR, d)[0], sofr(d)] for d in (basis_date, as_of)] +
          [[f"Treasury {t}", as_of, on_or_before(TSY, as_of)[0], cmt(t)] for t in ("1 Yr", "3 Yr", "5 Yr")])
    # ---------------------------------------------------------------- inputs from the financial model
    F = read(fin / "fin_fiscal_years.csv")[0]
    mrr = read(fin / "fin_monthly_runrate.csv")
    ukc = read(fin / "fin_uk_cost_per_employee.csv")
    hc = {int(r["year_end"]): r for r in read(fin / "fin_headcount.csv")}
    fy = int(F["fiscal_year"][2:])
    fig = {r["key"]: r for r in read(D / "company_figures.csv")}
    peers = read(D / "public_comps_sec_xbrl.csv")
    rev0, opex0, gm = num(F["revenue_usd"]), num(F["operating_cost_usd"]), num(F["gross_margin"])
    gms = sorted(num(p["ttm_gross_margin"]) for p in peers)
    gm_min, gm_max = gms[0], gms[-1]
    cph = num(F["cost_per_employee_usd"])
    us_avg = num(F["us_avg_participants"])
    last_y = max(y for y in hc)
    hc_end = round(num(hc[last_y]["us_hw_plan_501_active"]) / cfg["us_share"])
    rr_end, rr_start = num(mrr[-1]["fitted_run_rate_usd"]), num(mrr[0]["fitted_run_rate_usd"])
    by_fy = {u["period_end"]: u for u in ukc}
    w0 = num(by_fy[f"{cfg['wage_base_fy']}-01-31"]["cost_per_employee_gbp_annualised"])
    w1 = num(by_fy[f"{cfg['wage_last_fy']}-01-31"]["cost_per_employee_gbp_annualised"])
    wage = (w1 / w0) ** (1 / (cfg["wage_last_fy"] - cfg["wage_base_fy"])) - 1
    growth_rec = num(fig[cfg["growth_key"]]["value"])
    elasticity = (num(hc[last_y]["us_hw_plan_501_active"]) / num(hc[last_y - 1]["us_hw_plan_501_active"]) - 1) / growth_rec
    capacity = num(fig[cfg["capacity_key"]]["value"])
    eq_l = num(fig[cfg["valuation_key"]]["value"])
    px_l = num(fig[cfg["price_key"]]["value"])
    pm = {r["month"]: r for r in read(D / "nport_price_monthly.csv")}
    shares = round(eq_l / px_l)
    eq_np = round(num(pm[max(pm)]["median_price"]) * eq_l / px_l)
    # ---------------------------------------------------------------- debt and loan marks
    bdc = read(D / "credit_bdc_10k_positions.csv")
    unf = read(D / "credit_bdc_10k_unfunded.csv")
    spreads = {num(r["spread"]) for r in bdc if r["spread"]}
    assert len(spreads) == 1, spreads
    spread = spreads.pop()
    bdc_par = sum(num(r["par_usd"]) for r in bdc)
    loans = [dict(date=r["report_date"], month=r["report_date"][:7], house=r["fund_house"] or r["series_name"], fund=r["series_name"], key=r["fund_key"],
                  par=num(r["par_usd"]) or 0.0, value=num(r["fair_value_usd"]) or 0.0, rate=(num(r["annualized_rate_pct"]) or 0) / 100, funded=r["status"] == "funded",
                  maturity=r["maturity"], default=r["is_default"], arrears=r["interest_in_arrears"], title=r["issuer_title"]) for r in read(D / "nport_loans.csv")]
    for x in loans:
        x["price"] = x["value"] / x["par"] * 100 if x["par"] else None
    funded = [x for x in loans if x["funded"]]
    last: dict = {}
    for x in funded:
        if x["key"] not in last or x["date"] > last[x["key"]][0]["date"]:
            last[x["key"]] = [x]
        elif x["date"] == last[x["key"]][0]["date"]:
            last[x["key"]].append(x)
    np_last = [x for v in last.values() for x in v]
    debt0 = bdc_par + sum(x["par"] for x in np_last)
    debt_max = debt0 + capacity
    mat1, mat2 = dt.date.fromisoformat(C["maturity_first"]), dt.date.fromisoformat(C["maturity_second"])
    grp = lambda mat: 2 if str(mat2.year) in mat else 1                                             # noqa: E731
    par1 = sum(num(r["par_usd"]) for r in bdc if grp(r["maturity"]) == 1) + sum(x["par"] for x in np_last if grp(x["maturity"]) == 1)
    par2 = debt0 - par1
    m_years = (par1 * (mat1 - as_of).days + par2 * (mat2 - as_of).days) / 365.25 / debt0
    lastout = [x for x in loans if "LAST OUT" in x["title"].upper()]
    lastout_par = max((sum(x["par"] for x in lastout if x["month"] == m) for m in {x["month"] for x in lastout}), default=0.0)

    def floater(dm, s, base, years):
        n = max(int(round(years * 4)), 1)
        c, k = (base + s) / 4 * 100, 1 + (base + dm) / 4
        return sum(c / k ** i for i in range(1, n + 1)) + 100 / k ** n

    def solve_dm(price, s, base, years):
        lo, hi = -0.05, 0.30
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if floater(mid, s, base, years) > price else (lo, mid)
        return (lo + hi) / 2

    marks = []
    for m in sorted({x["month"] for x in funded}):
        xs = [x for x in funded if x["month"] == m]
        rep = max(x["date"] for x in xs)
        dr = dt.date.fromisoformat(rep)
        par = sum(x["par"] for x in xs)
        vw = sum(x["value"] for x in xs) / par * 100
        pr = sorted(x["price"] for x in xs)
        yrs = sum((dt.date.fromisoformat(x["maturity"]) - dr).days / 365.25 * x["par"] for x in xs) / par
        base, cp = sofr(dr), float(np.median([x["rate"] for x in xs]))
        marks.append(dict(month=m, report=rep, positions=len(xs), funds=len({x["fund"] for x in xs}), houses=len({x["house"] for x in xs}), par=par, vw=vw,
                          median=float(np.median(pr)), lo=pr[0], hi=pr[-1], coupon=cp, sofr=base, gap=cp - base, years=yrs, dm=solve_dm(vw, spread, base, yrs), q=len(xs) >= 10))
    qm = [x for x in marks if x["q"]]
    write("credit_loan_marks_monthly.csv", ["month", "latest_report_date", "funded_positions", "funds", "fund_houses", "par_usd", "value_weighted_price_pct_par", "median_price",
                                            "min_price", "max_price", "median_coupon", "sofr_on_report_date", "coupon_minus_sofr", "par_weighted_years_to_maturity", "discount_margin"],
          [[x["month"], x["report"], x["positions"], x["funds"], x["houses"], round(x["par"]), round(x["vw"], 3), round(x["median"], 3), round(x["lo"], 3), round(x["hi"], 3),
            round(x["coupon"], 5), x["sofr"], round(x["gap"], 5), round(x["years"], 2), round(x["dm"], 5)] for x in marks])
    dm_last = qm[-1]["dm"]
    lgd, grid = C["lgd"], C["lgd_grid"]
    write("credit_market_implied.csv", ["as_of_report", "discount_margin", "lgd", "risk_neutral_hazard", "pd_1y", "pd_3y", "pd_5y"],
          [[qm[-1]["report"], round(dm_last, 5), l, round(dm_last / l, 5)] + [round(1 - math.exp(-dm_last / l * t), 5) for t in (1, 3, 5)] for l in grid])

    # ---------------------------------------------------------------- metrics and scorecard
    sofr_b = sofr(basis_date)

    def metrics(name, rev, opex, debt, base, equity, g=None):
        g = gm if g is None else g
        e = rev * g - opex
        i = debt * (base + spread)
        return dict(name=name, rev=rev, opex=opex, gm=g, ebitda=e, margin=e / rev, debt=debt, rate=base, interest=i, lev=debt / e if e > 0 else None, cov=e / i if e > 0 else None,
                    fcf=e - i, fcf_debt=(e - i) / debt, equity=equity, ltv=debt / equity)

    bases = [metrics("TTM (primary)", rev0, opex0, debt0, sofr_b, eq_l), metrics("Capacity drawn", rev0, opex0, debt_max, sofr_b, eq_l),
             metrics("Run-rate exit", rr_end, hc_end * cph, debt0, sofr_b, eq_l)]
    base = bases[0]
    write("credit_metrics_by_basis.csv", ["basis", "revenue_usd", "gross_margin", "operating_cost_usd", "ebitda_proxy_usd", "ebitda_margin", "debt_usd", "sofr", "interest_usd",
                                          "debt_to_ebitda", "ebitda_to_interest", "fcf_proxy_after_interest_usd", "fcf_to_debt", "equity_value_usd", "debt_to_equity_value"],
          [[b["name"], round(b["rev"]), b["gm"], round(b["opex"]), round(b["ebitda"]), round(b["margin"], 4), round(b["debt"]), b["rate"], round(b["interest"]),
            round(b["lev"], 3) if b["lev"] else "", round(b["cov"], 3) if b["cov"] else "", round(b["fcf"]), round(b["fcf_debt"], 4), round(b["equity"]), round(b["ltv"], 4)] for b in bases])
    # business profile: five sub-scores, each read off a rule table in the config (metric levels at notches 3, 6, 9, 12, 15)
    fy_vals = {r["fiscal_year"]: num(r["form_d_equity_non_bc_usd"]) for r in read(fin / "fin_funding_by_fiscal_year.csv")}
    peer_rev = float(np.median([num(p["ttm_rev_usd"]) for p in peers]))
    rule_x = {"market_position": rev0 / peer_rev, "growth": growth_rec, "funding_access": sum(v for k, v in fy_vals.items() if int(k[2:]) >= fy) / rev0, "gross_margin": gm}
    rule_n = [3, 6, 9, 12, 15]
    bp_parts = {k: float(np.interp(rule_x[k], C["business_rules"][k][::-1], rule_n[::-1])) for k in rule_x}
    bp_parts["profit_record"] = None
    factors, ns0 = C["factors"], C["letters"]
    adj0 = sum(C["adjustments"].values())

    def factor(key, m, bp=None):
        f = next(x for x in factors if x[0] == key)
        if key == "business":
            return bp0 if bp is None else bp
        v = {"scale": m["rev"], "margin": m["margin"], "leverage": m["lev"], "coverage": m["cov"], "fcf_debt": m["fcf_debt"], "ltv": m["ltv"]}[key]
        if v is None:
            return 19.0
        xs, ns = list(f[3]), list(ns0)
        if f[4] == "log":
            xs, v = [math.log(t) for t in xs], math.log(v)
        if xs[0] > xs[-1]:
            xs, ns = xs[::-1], ns[::-1]
        return float(np.interp(v, xs, ns))

    xs_m, ns_m = next(f for f in factors if f[0] == "margin")[3][::-1], ns0[::-1]
    bp_parts["profit_record"] = float(np.interp(base["margin"], xs_m, ns_m))
    bp0 = sum(bp_parts.values()) / len(bp_parts)
    write("credit_business_profile.csv", ["sub_factor", "metric", "notch"], [[k, round(rule_x.get(k, base["margin"]), 4), round(v, 2)] for k, v in bp_parts.items()])

    def score(m, weights=None, bp=None, adj=None):
        parts = [(f[0], factor(f[0], m, bp), (weights or {}).get(f[0], f[2])) for f in factors]
        anchor = sum(n * w for _, n, w in parts)
        final = min(anchor + (adj0 if adj is None else adj), 21)
        fl = 0.0
        if m["ebitda"] <= m["interest"]:
            fl = C["coverage_floor"]
        if m.get("short"):
            fl = C["liquidity_floor"]
        final = max(final, fl)
        return dict(parts=parts, anchor=anchor, final=final, rating=letter(final))

    for b in bases:
        b.update(score(b))
    final_r, final_n = base["rating"], NOTCH[base["rating"]]
    fx = lambda **k: metrics("x", k.get("rev", base["rev"]), k.get("opex", base["opex"]), k.get("debt", base["debt"]), base["rate"], k.get("equity", base["equity"]), k.get("g"))   # noqa: E731
    hc_cost = lambda s: us_avg / s * cph                                                            # noqa: E731
    sens = [("Base", score(base)), ("Gross margin lowest peer", score(fx(g=gm_min))), ("Gross margin highest peer", score(fx(g=gm_max))),
            ("U.S. share 55%", score(fx(opex=hc_cost(0.55)))), ("U.S. share 75%", score(fx(opex=hc_cost(0.75)))), ("Cost per employee +10%", score(fx(opex=base["opex"] * 1.1))),
            ("Cost per employee -10%", score(fx(opex=base["opex"] * 0.9))), ("Capacity drawn", score(fx(debt=debt_max))),
            ("Equal factor weights", score(base, weights={f[0]: 1 / len(factors) for f in factors})), ("Business profile 2 notches weaker", score(base, bp=bp0 + 2)),
            ("Business profile 2 notches stronger", score(base, bp=bp0 - 2)), ("No notch adjustments", score(base, adj=0.0)), ("Equity = N-PORT implied", score(fx(equity=eq_np)))]
    write("credit_rating_grid.csv", ["gross_margin", "us_share", "final_notch", "synthetic_rating"],
          [[round(g_, 4), s_, round(x["final"], 3), x["rating"]] for g_, s_, x in ((g_, s_, score(fx(g=g_, opex=hc_cost(s_)))) for g_ in (gm_min, gm, gm_max) for s_ in (0.55, 0.65, 0.75))])
    write("credit_scorecard_sensitivity.csv", ["variant", "anchor_notch", "final_notch", "synthetic_rating", "pd_1y", "pd_5y"],
          [[n, round(x["anchor"], 3), round(x["final"], 3), x["rating"], round(pd_at(NOTCH[x["rating"]], 1), 6), round(pd_at(NOTCH[x["rating"]], 5), 6)] for n, x in sens])
    write("credit_scorecard.csv", ["basis", "factor", "weight", "factor_notch", "factor_letter", "weighted_notch"],
          [[b["name"], f[1], f[2], round(n, 2), letter(n), round(n * f[2], 3)] for b in bases for f, (_, n, _) in zip(factors, b["parts"])] +
          [[b["name"], "anchor (weighted sum)", 1.0, round(b["anchor"], 2), letter(b["anchor"]), round(b["anchor"], 3)] for b in bases] +
          [[b["name"], "adjustments", "", adj0, "", ""] for b in bases] + [[b["name"], "final synthetic rating", "", round(b["final"], 2), b["rating"], ""] for b in bases])
    # ---------------------------------------------------------------- Merton
    qe = [m for m in sorted(pm) if m[5:] in ("03", "06", "09", "12") and m >= "2021-03"]
    lr = np.array([math.log(num(pm[qe[i]]["median_price"]) / num(pm[qe[i - 1]]["median_price"])) for i in range(1, len(qe))])
    rho = float(np.corrcoef(lr[1:], lr[:-1])[0, 1])
    sig_obs = float(lr.std(ddof=1)) * 2
    sig = sig_obs * math.sqrt((1 + rho) / (1 - rho)) if rho > 0 else sig_obs

    def merton(E, Dbt, se, r, T):
        V, sv = E + Dbt * math.exp(-r * T), se * E / (E + Dbt)
        for _ in range(500):
            for _ in range(100):
                d1 = (math.log(V / Dbt) + (r + sv * sv / 2) * T) / (sv * math.sqrt(T))
                d2 = d1 - sv * math.sqrt(T)
                e = V * N.cdf(d1) - Dbt * math.exp(-r * T) * N.cdf(d2) - E
                V -= e / N.cdf(d1)
                if abs(e) < 1e-3:
                    break
            new = se * E / (V * N.cdf(d1))
            stop = abs(new - sv) < 1e-12
            sv = new
            if stop:
                break
        d2 = (math.log(V / Dbt) + (r - sv * sv / 2) * T) / (sv * math.sqrt(T))
        return dict(V=V, sv=sv, dd=d2, pd=N.cdf(-d2))

    mer = []
    for en, E in (("Series L valuation", eq_l), ("N-PORT implied", eq_np)):
        for dn, dv in (("visible funded debt", debt0), ("capacity drawn", debt_max)):
            for sg in C["eq_vol_grid"]:
                s = sg or sig
                for T, ten in ((1, "1 Yr"), (3, "3 Yr"), (5, "5 Yr")):
                    mer.append(dict(en=en, E=E, dn=dn, D=dv, sig=s, lab="N-PORT" if sg is None else f"{sg:.0%}", T=T, r=cmt(ten), **merton(E, dv, s, cmt(ten), T)))
    write("credit_merton.csv", ["equity_basis", "equity_value_usd", "debt_basis", "debt_usd", "equity_vol", "vol_label", "horizon_years", "risk_free", "asset_value_usd", "asset_vol",
                                "distance_to_default", "pd_risk_neutral"],
          [[x["en"], round(x["E"]), x["dn"], round(x["D"]), round(x["sig"], 4), x["lab"], x["T"], x["r"], round(x["V"]), round(x["sv"], 4), round(x["dd"], 3), x["pd"]] for x in mer])
    pick = lambda e, d, l, T: next(x for x in mer if x["en"] == e and x["dn"] == d and x["lab"] == l and x["T"] == T)    # noqa: E731
    pds = [dict(h=h, r=pd_at(final_n, h), up=pd_at(final_n - 2, h), down=pd_at(final_n + 2, h), m=pick("Series L valuation", "visible funded debt", "N-PORT", h)["pd"] if h != 10 else None,
                m75=pick("Series L valuation", "capacity drawn", "75%", h)["pd"] if h != 10 else None, mk=1 - math.exp(-dm_last / lgd * h)) for h in (1, 3, 5, 10)]
    ref_pd = max(pds[0]["r"], C["pd_floor"])
    write("credit_pd_summary.csv", ["horizon_years", f"rating_implied_{final_r}", f"rating_implied_{RATINGS[final_n - 3]}", f"rating_implied_{RATINGS[final_n + 1]}", "merton_series_l_nport_vol", "merton_capacity_drawn_75pct_vol", "market_implied_upper_bound_lgd45"],
          [[x["h"], round(x["r"], 6), round(x["up"], 6), round(x["down"], 6), "" if x["m"] is None else x["m"], "" if x["m75"] is None else x["m75"], round(x["mk"], 5)] for x in pds])

    # ---------------------------------------------------------------- expected loss and capital
    def irb(pd_, l, m):
        r = 0.12 + 0.12 * math.exp(-50 * pd_)
        b = (0.11852 - 0.05478 * math.log(pd_)) ** 2
        k = (l * N.cdf((N.inv_cdf(pd_) + math.sqrt(r) * N.inv_cdf(0.999)) / math.sqrt(1 - r)) - l * pd_) * (1 + (min(max(m, 1), 5) - 2.5) * b) / (1 - 1.5 * b)
        return k, r, b

    el = []
    for nn in (final_n - 2, final_n, final_n + 2):
        p1 = max(pd_at(nn, 1), C["pd_floor"])
        for l in grid:
            k, r, b = irb(p1, l, m_years)
            el.append([RATINGS[nn - 1], round(p1, 6), l, round(m_years, 2), round(r, 4), round(b, 4), round(k, 5), round(k * 12.5, 4), round(debt0), round(p1 * l * debt0),
                       round(pd_at(nn, m_years) * l * debt0), round(k * 12.5 * debt0), round(k * debt0), round((spread - p1 * l) / k, 3)])
    write("credit_el_capital.csv", ["rating", "pd_1y_floored", "lgd", "effective_maturity_years", "correlation_R", "maturity_adj_b", "capital_K", "risk_weight", "ead_usd",
                                    "expected_loss_1y_usd", "lifetime_expected_loss_usd", "rwa_usd", "capital_usd", "spread_less_el_over_K"], el)
    # ---------------------------------------------------------------- stress tests
    S = C["stress"]
    R0 = base["rate"]
    sc = [("S0", metrics("S0", rev0, opex0, debt0, R0, eq_l)), ("S1", metrics("S1", rev0, opex0, debt0, R0 + S["rate_up"], eq_l)), ("S2", metrics("S2", rev0, opex0, debt_max, R0, eq_l)),
          ("S3", metrics("S3", rev0, opex0, debt0, R0, eq_l, round(gm_min, 4))), ("S4", metrics("S4", rev0, opex0 * (1 + S["stall_headcount"]) * (1 + wage), debt0, R0, eq_l)),
          ("S5", metrics("S5", rev0, opex0, debt0, R0, eq_l * (1 - S["value_cut"]))),
          ("S6", metrics("S6", rev0 * (1 + S["severe_rev"]), opex0, debt_max, R0 + S["severe_rate"], eq_l * (1 - S["value_cut"]), S["severe_gm"])),
          ("S7", metrics("S7", rev0, opex0, debt0 * C["hidden_debt_mult"] + capacity, R0, eq_l))]
    srows = []
    for sid, m in sc:
        m.update(score(m))
        n = NOTCH[m["rating"]]
        srows.append([sid, m, pd_at(n, 1), pd_at(n, 5), merton(m["equity"], m["debt"], sig, cmt("1 Yr"), 1)["pd"], merton(m["equity"], m["debt"], 0.75, cmt("5 Yr"), 5)["pd"]])
    write("credit_stress_tests.csv", ["id", "revenue_usd", "ebitda_proxy_usd", "ebitda_margin", "debt_usd", "interest_usd", "debt_to_ebitda", "ebitda_to_interest", "fcf_to_debt",
                                      "debt_to_equity_value", "scorecard_notch", "notch_change_vs_base", "synthetic_rating", "pd_1y_rating_implied", "pd_5y_rating_implied",
                                      "merton_pd_1y_nport_vol", "merton_pd_5y_75pct_vol"],
          [[s[0], round(s[1]["rev"]), round(s[1]["ebitda"]), round(s[1]["margin"], 4), round(s[1]["debt"]), round(s[1]["interest"]), round(s[1]["lev"], 3) if s[1]["lev"] else "n/m",
            round(s[1]["cov"], 3) if s[1]["cov"] else "n/m", round(s[1]["fcf_debt"], 4), round(s[1]["ltv"], 4), round(s[1]["final"], 2), round(s[1]["final"] - sc[0][1]["final"], 2),
            s[1]["rating"], round(s[2], 6), round(s[3], 6), s[4], s[5]] for s in srows])
    # ---------------------------------------------------------------- migration
    one = [r for r in tm_rows if r["horizon_years"] == "1"]
    states = [r["from_rating"] for r in one] + ["D"]
    ix = {s: i for i, s in enumerate(states)}
    tm = np.zeros((len(states), len(states)))
    cols = RATINGS + ["Default"]
    for r in one:
        i, wr = ix[r["from_rating"]], float(r["Withdrawn/Paid Off"] or 0)
        for k in cols:
            tm[i, ix["D"] if (k == "Default" or k not in ix) else ix[k]] += float(r[k] or 0) / (1 - wr)
    tm[ix["D"], ix["D"]] = 1.0
    tm = tm / tm.sum(axis=1, keepdims=True)

    def markov(rt, y):
        v, res = np.eye(len(states))[ix[rt]], []
        for _ in range(y):
            v = v @ tm
            res.append(float(v[ix["D"]]))
        return res

    mkr = [RATINGS[final_n - 3], final_r, RATINGS[final_n + 1]]
    mk = {r: markov(r, 10) for r in mkr}
    write("credit_transition_matrix_1y_wr_adjusted.csv", ["from \\ to"] + states, [[s] + [round(float(x), 6) for x in tm[i]] for i, s in enumerate(states)])
    write("credit_migration_markov.csv", ["years"] + [f"markov_pd_{r}" for r in mkr] + [f"constant_hazard_pd_{r}" for r in mkr],
          [[h] + [round(mk[r][h - 1], 6) for r in mkr] + [round(pd_at(NOTCH[r], h), 6) for r in mkr] for h in range(1, 11)])
    # ---------------------------------------------------------------- projections
    sofr_p = sofr(as_of)
    rate_p = sofr_p + spread
    years = C["proj_years"]
    g_rec = (rr_end / rr_start) ** 0.25 - 1

    def year_rev(gq):
        r, rr, res = (1 + gq) ** (-1 / 3), rr_end, 0.0
        for _ in range(4):
            rr *= 1 + gq
            res += rr / 12 * (1 + r + r * r)
        return res

    g_base, g_flat = year_rev(g_rec / 2) / rev0 - 1, year_rev(0.0) / rev0 - 1
    path = [g_base]
    for _ in range(4):
        path.append(max(path[-1] * C["growth_decay"], C["growth_floor"]))

    def scen(s):
        return dict(g=[g_flat if x is None else x for x in s["g"]], gm=[gm_min if x == "min" else x for x in s["gm"]], hc=s["hc"], eq=s["eq"])

    SCN = {"Base": dict(g=path, gm=[gm] * 5, hc=[None] * 5, eq=1.0), "Downside": scen(C["downside"]), "Severe": scen(C["severe"])}

    def project(s):
        rev, opex, cash, drawn = rev0, opex0, 0.0, 0.0
        tl = [[par1, mat1], [par2, mat2]]
        res = []
        for t, y in enumerate(years):
            g, gmt = s["g"][t], s["gm"][t]
            h = elasticity * g if s["hc"][t] is None else s["hc"][t]
            rev_n, opex_n = rev * (1 + g), opex * (1 + h) * (1 + wage)
            e = rev_n * gmt - opex_n
            i = (sum(a for a, _ in tl) + drawn) * rate_p
            tax = C["tax_rate"] * max(e - i, 0.0) if y >= C["tax_from"] else 0.0
            f = e - i - tax
            cash += f
            if cash < 0:
                dd = min(-cash, capacity - drawn)
                drawn += dd
                cash += dd
            elif drawn > 0:
                rr_ = min(cash, drawn)
                drawn -= rr_
                cash -= rr_
            short = cash < -1.0
            for tr in tl:
                if tr[0] > 0 and dt.date(y - 1, 2, 1) <= tr[1] <= dt.date(y, 1, 31):
                    now = sum(a for a, _ in tl) + drawn
                    if e > 0 and now / e <= C["refi_max_lev"]:
                        tr[1] = tr[1].replace(year=tr[1].year + 5)
                    elif cash + capacity - drawn >= tr[0]:
                        pay = tr[0]
                        use = min(cash, pay)
                        cash -= use
                        drawn += pay - use
                        tr[0] = 0.0
                    else:
                        short = True
            debt = sum(a for a, _ in tl) + drawn
            eq = eq_l * rev_n / rev0 * s["eq"]
            m = dict(rev=rev_n, ebitda=e, margin=e / rev_n, debt=debt, interest=i, lev=debt / e if e > 0 else None, cov=e / i if e > 0 else None, fcf_debt=f / debt if debt else 1.0, ltv=debt / eq, equity=eq, short=short)
            sr = score(m)
            res.append(dict(y=y, g=g, gm=gmt, h=h, rev=rev_n, opex=opex_n, e=e, margin=e / rev_n, i=i, tax=tax, f=f, cash=cash, drawn=drawn, debt=debt, lev=m["lev"], cov=m["cov"],
                            fd=m["fcf_debt"], ltv=m["ltv"], final=sr["final"], rating=sr["rating"], pd1=pd_at(NOTCH[sr["rating"]], 1), short=short))
            rev, opex = rev_n, opex_n
        return res

    proj = {k: project(v) for k, v in SCN.items()}
    write("credit_projections.csv", ["scenario", "fiscal_year", "revenue_growth", "gross_margin", "headcount_growth", "revenue_usd", "operating_cost_usd", "ebitda_proxy_usd", "ebitda_margin", "interest_usd",
                                     "cash_taxes_usd", "fcf_usd", "modelled_cash_usd", "capacity_drawn_usd", "debt_usd", "debt_net_of_modelled_cash_usd", "debt_to_ebitda", "ebitda_to_interest", "fcf_to_debt",
                                     "debt_to_equity_value", "scorecard_notch", "synthetic_rating", "pd_1y"],
          [[k, f"FY{p['y']}", round(p["g"], 4), round(p["gm"], 4), round(p["h"], 4), round(p["rev"]), round(p["opex"]), round(p["e"]), round(p["margin"], 4), round(p["i"]), round(p["tax"]), round(p["f"]),
            round(p["cash"]), round(p["drawn"]), round(p["debt"]), round(p["debt"] - p["cash"]), round(p["lev"], 3) if p["lev"] else "n/m", round(p["cov"], 3) if p["cov"] else "n/m", round(p["fd"], 4),
            round(p["ltv"], 4), round(p["final"], 2), p["rating"], round(p["pd1"], 6)] for k in proj for p in proj[k]])
    # ---------------------------------------------------------------- Monte Carlo
    M = C["mc"]

    def run_mc(sigma):
        n = M["n"]
        rng = np.random.default_rng(M["seed"])
        b = SCN["Base"]
        rev, opex = np.full(n, rev0), np.full(n, opex0)
        cash, drawn, t1 = np.zeros(n), np.zeros(n), np.full(n, par1)
        alive, eps, prev = np.ones(n, bool), np.zeros(n), None
        neg, cum = np.zeros(n, bool), []
        for t, y in enumerate(years):
            eps = M["phi"] * eps + sigma * rng.standard_normal(n)
            g = np.maximum(b["g"][t] + eps, -0.6)
            gmt = np.clip(b["gm"][t] + M["gm_sd"] * rng.standard_normal(n), 0.55, 0.88)
            h = np.maximum(elasticity * (g if t == 0 else prev), -0.10)
            rev, opex = rev * (1 + g), opex * (1 + h) * (1 + wage)
            e = rev * gmt - opex
            neg |= e <= 0
            i = (t1 + par2 + drawn) * rate_p
            tax = C["tax_rate"] * np.maximum(e - i, 0.0) if y >= C["tax_from"] else 0.0
            cash = cash + e - i - tax
            dr = np.minimum(np.maximum(-cash, 0.0), capacity - drawn)
            drawn += dr
            cash += dr
            rp = np.where(cash > 0, np.minimum(cash, drawn), 0.0)
            drawn -= rp
            cash -= rp
            fail = cash < -1.0
            if y == mat1.year:
                refi = (e > 0) & ((t1 + par2 + drawn) <= C["refi_max_lev"] * np.maximum(e, 1e-9))
                fail |= ~refi & (cash + capacity - drawn < t1)
            alive &= ~fail
            cum.append(float(1 - alive.mean()))
            prev = g
        lev = np.where(e > 0, (t1 + par2 + drawn) / np.where(e > 0, e, 1.0), np.inf)
        return [sigma, n, M["seed"]] + [round(v, 6) for v in cum] + [round(float(neg.mean()), 4), round(float(np.mean(lev > C["refi_max_lev"])), 4)] + \
            [round(v) for v in np.percentile(rev, [5, 50, 95])] + [round(v, 4) for v in np.percentile(e / rev, [5, 50, 95])]

    mc = [run_mc(s) for s in M["sigmas"]]
    write("credit_monte_carlo.csv", ["sigma", "paths", "seed", "cum_default_FY2027", "cum_default_FY2028", "cum_default_FY2029", "cum_default_FY2030", "cum_default_FY2031", "share_paths_ebitda_negative_any_year",
                                     "share_debt_to_ebitda_above_6x_FY2031", "revenue_FY2031_p5_usd", "revenue_FY2031_p50_usd", "revenue_FY2031_p95_usd", "ebitda_margin_FY2031_p5", "ebitda_margin_FY2031_p50",
                                     "ebitda_margin_FY2031_p95"], mc)
    # ---------------------------------------------------------------- recovery, CECL, capacity, pricing, liquidity
    dy = min(proj["Severe"], key=lambda p: p["e"])
    claims = debt_max * (1 + rate_p * C["accrued_months"] / 12)
    rec = []
    for mult, w in C["ev_mults"]:
        ev = dy["rev"] * mult
        dist = ev * (1 - C["admin_share"])
        r = min(1.0, dist / claims)
        rec.append(dict(mult=mult, w=w, ev=ev, dist=dist, rec=r, lgd=1 - r))
    lgd_m = sum(r["w"] * r["lgd"] for r in rec)
    write("credit_recovery_waterfall.csv", ["ev_to_revenue_multiple", "weight", "revenue_at_default_usd", "enterprise_value_usd", "after_admin_claims_usd", "first_lien_claims_usd", "first_lien_recovery", "lgd"],
          [[r["mult"], r["w"], round(dy["rev"]), round(r["ev"]), round(r["dist"]), round(claims), round(r["rec"], 4), round(r["lgd"], 4)] for r in rec])
    lam = hazard(final_n)
    y1, y2 = (mat1 - as_of).days / 365.25, (mat2 - as_of).days / 365.25

    def ecl(ead, yrs, l):
        tot, t0 = 0.0, 0.0
        while t0 < yrs - 1e-9:
            t1 = min(t0 + 1, yrs)
            tot += (math.exp(-lam * t0) - math.exp(-lam * t1)) * l * ead / (1 + rate_p) ** ((t0 + t1) / 2)
            t0 = t1
        return tot

    cecl = []
    for n, l in (("Reference LGD", lgd), ("Modelled recovery LGD", lgd_m)):
        life = ecl(par1, y1, l) + ecl(par2, y2, l)
        cecl.append([n, round(l, 4), round(lam, 6), round(rate_p, 4), round(debt0), round(ecl(par1, 1, l) + ecl(par2, 1, l)), round(life), round(life / debt0, 6)])
    write("credit_cecl.csv", ["lgd_basis", "lgd", "annual_hazard", "discount_rate", "ead_usd", "ecl_12m_usd", "ecl_lifetime_usd", "ecl_lifetime_pct_ead"], cecl)

    def notch_with(debt=debt0, scale=1.0):
        e = base["ebitda"] * scale
        return score(metrics("x", base["rev"], base["rev"] * gm - e, debt, base["rate"], eq_l))["final"]

    def solve(fn, lo, hi, target, rising):
        for _ in range(100):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if (fn(mid) <= target) == rising else (lo, mid)
        return (lo + hi) / 2

    cap = []
    for g, edge in C["grade_edges"]:
        if notch_with() > edge:
            cap.append([g, edge, "already below this grade", "", "", ""])
            continue
        dm_ = solve(lambda d: notch_with(debt=d), 0.0, 80e9, edge, True)
        sm = solve(lambda s: notch_with(scale=s), 0.0, 5.0, edge, False)
        cap.append([g, edge, round(dm_), round(dm_ - debt0), round(base["ebitda"] * sm), round(1 - sm, 4)])
    write("credit_debt_capacity.csv", ["lowest_grade_kept", "notch_edge", "max_funded_debt_usd", "headroom_over_current_debt_usd", "min_ebitda_usd", "ebitda_cushion_pct"], cap)
    price = []
    for n, l in (("Reference LGD", lgd), ("Modelled LGD", lgd_m)):
        k = irb(ref_pd, l, m_years)[0]
        req = ref_pd * l + C["hurdle"] * k
        price.append([n, round(ref_pd, 6), round(l, 4), round(ref_pd * l, 6), round(k, 5), C["hurdle"], round(C["hurdle"] * k, 5), round(req, 5), round(dm_last, 5), round(dm_last - req, 5)])
    write("credit_pricing.csv", ["lgd_basis", "pd_1y", "lgd", "expected_loss_rate", "capital_K", "hurdle", "capital_charge", "required_spread", "observed_discount_margin", "spread_not_explained_by_credit_risk"], price)
    return dict(rating=final_r, base=base, bases=bases, pds=pds, dm=dm_last, sig=sig, m_years=m_years, lastout=lastout_par, n_default=sum(x["default"] == "Y" for x in loans),
                n_arrears=sum(x["arrears"] == "Y" for x in loans), n_loans=len(loans), price_lo=min(x["lo"] for x in qm), price_hi=max(x["hi"] for x in qm), qm=qm, srows=srows, proj=proj, mc=mc,
                lgd_m=lgd_m, ref_pd=ref_pd, cap=cap, sens=sens, unfunded=sum(num(r["unfunded_usd"]) for r in unf), bdc_par=bdc_par, n_bdc=len({r["fund"] for r in bdc}), debt=debt0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--fin", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = run(Path(a.data), Path(a.fin), json.load(open(a.config, encoding="utf-8")), Path(a.out))
    b = res["base"]
    print(f"rating {res['rating']} anchor {b['anchor']:.2f} final {b['final']:.2f}; PD1 {res['pds'][0]['r']:.2%} PD5 {res['pds'][2]['r']:.2%}; DM {res['dm']:.3%}; MC 5y {res['mc'][1][7]:.2%}")
