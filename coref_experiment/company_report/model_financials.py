#!/usr/bin/env python3
"""Financial model from public records: revenue curve, staff, cost per employee, EBITDA and FCF proxies, funding, valuation, year by year.
Every value comes from a CSV in --data or from the assumptions in --config. Nothing here is company-specific.

    python model_financials.py --data DIR --config model_config/databricks.json --out OUT
"""
from __future__ import annotations

import argparse
import calendar
import collections
import csv
import datetime as dt
import json
from pathlib import Path

import numpy as np


def rows(folder: Path, name: str) -> list[dict]:
    with open(folder / "data" / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(x):
    return float(x) if x not in (None, "", "None") else None


def day(x: str) -> dt.date:
    return dt.date.fromisoformat(x)


def write(out: Path, name: str, header: list, body: list) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(body)


def month_ends(a: dt.date, z: dt.date) -> list[dt.date]:
    res, y, m = [], a.year, a.month
    while True:
        d = dt.date(y, m, calendar.monthrange(y, m)[1])
        if d > z:
            return res
        res.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class Model:
    def __init__(self, data: Path, cfg: dict):
        self.data, self.c = Path(data), cfg
        self.fig = {r["key"]: r for r in rows(self.data, "company_figures.csv")}
        self.me = month_ends(day(cfg["fit_start"]), day(cfg["fit_end"]))
        self.ix = {d: i for i, d in enumerate(self.me)}

    def fy(self, d: dt.date) -> int:
        return d.year + 1 if d.month > self.c["fy_end_month"] else d.year

    def value(self, key: str) -> float:
        return float(self.fig[key]["value"])

    # ---------------------------------------------------------------- revenue
    def fit(self, first_value=None, first_date=None, growth=None, lam=None):
        c = self.c
        first_value = first_value or self.value(c["rr_first_key"])
        first_date = first_date or day(c["rr_first_date"])
        growth = growth if growth is not None else self.value(c["growth_key"])
        lam = lam if lam is not None else c["smooth_lambda"]
        last = self.value(c["rr_last_key"])
        n, end = len(self.me), self.me[-1]
        A, y, w = [], [], []
        for d, v in ((first_date, first_value), (end, last)):
            r = np.zeros(n)
            r[self.ix[d]] = 1
            A.append(r), y.append(np.log(v)), w.append(1.0)
        r = np.zeros(n)
        r[-1], r[-13] = 1, -1
        A.append(r), y.append(np.log(1 + growth)), w.append(1.0)
        for i in range(1, n - 1):
            r = np.zeros(n)
            r[i - 1], r[i], r[i + 1] = 1, -2, 1
            A.append(r), y.append(0.0), w.append(lam)
        A, y, s = np.array(A), np.array(y), np.sqrt(np.array(w))
        x = np.linalg.lstsq(A * s[:, None], y * s, rcond=None)[0]
        return np.exp(x), (A @ x - y)[:3]

    def year_rev(self, rr, quarter=False) -> float:
        fy = self.c["fit_year"]
        inside = [d for d in self.me if self.fy(d) == fy]
        if quarter:
            return float(sum(rr[self.ix[d]] / 4 for d in inside if (d.month - self.c["fy_end_month"]) % 3 == 0))
        return float(sum(rr[self.ix[d]] / 12 for d in inside))

    def run(self) -> dict:
        c, D = self.c, self.data
        rr, resid = self.fit()
        fy, fq = c["fit_year"], f"FY{c['fit_year']}"
        rev, rev_q = self.year_rev(rr), self.year_rev(rr, True)
        cases = [("Base case", rev, "fitted curve, run-rate = month x 12")]
        cases += [(f"Smoothing lambda = {x}", self.year_rev(self.fit(lam=x)[0]), "curve stiffness") for x in c["smooth_alt"]]
        cases += [("First statement one month later", self.year_rev(self.fit(first_date=day(c["rr_first_date_alt"]))[0]), "statement date"),
                  ("First statement read higher", self.year_rev(self.fit(first_value=c["rr_first_alt_value"])[0]), "threshold above the floor"),
                  ("Growth read higher", self.year_rev(self.fit(growth=c["growth_alt"])[0]), "lower starting point"),
                  ("Run-rate = quarter x 4", rev_q, "alternative definition")]
        low, high = min(x[1] for x in cases), max(x[1] for x in cases)
        # ------------------------------------------------------------ staff
        health = {int(r["FORM_PLAN_YEAR_BEGIN_DATE"][:4]): int(r["TOT_ACTIVE_PARTCP_CNT"]) for r in rows(D, "dol_form5500.csv")
                  if r["SPONS_DFE_PN"] == c["health_plan"]}
        k401 = {int(r["plan_year"]): r for r in rows(D, "plan_participants.csv") if r["plan_number"] == c["k401_plan"]}
        y0, y1 = fy - 2, fy - 1
        us_avg = (health[y0] + health[y1]) / 2
        share = c["us_share"]
        hc = us_avg / share
        # ------------------------------------------------------------ cost per employee (foreign subsidiary)
        fx: dict = {}
        with open(D / "sources" / "treasury" / "treasury_rates_of_exchange_GBP.json", encoding="utf-8") as fh:
            for r in sorted(json.load(fh)["data"], key=lambda r: r["effective_date"]):
                fx[r["record_date"]] = 1 / float(r["exchange_rate"])

        def fx_avg(a, z):
            v = [x for k, x in fx.items() if a <= day(k) <= z]
            return sum(v) / len(v) if v else None

        uk = []
        for r in rows(D, "uk_companies_house_accounts.csv"):
            end, mths = day(r["period_end"]), int(r["months"])
            cost = num(r["turnover_gbp"]) - num(r["operating_profit_gbp"])
            sbc = num(r["share_based_payment_gbp"]) or 0.0
            per = (cost - sbc) / num(r["avg_employees"]) * 12 / mths
            rate = fx_avg(dt.date(end.year - 1, end.month, 1), end) or fx_avg(end - dt.timedelta(days=400), end)
            uk.append(dict(end=end, months=mths, turnover=num(r["turnover_gbp"]), cost=cost, sbc=sbc, ex=cost - sbc, staff=int(r["avg_employees"]),
                           per=per, fx=rate, per_usd=per * rate if rate else None, markup=num(r["operating_profit_gbp"]) / cost,
                           pay=sum(num(r[k]) or 0 for k in ("wages_salaries_gbp", "social_security_gbp", "pension_gbp")), people=sum(num(r[k]) or 0 for k in ("wages_salaries_gbp", "social_security_gbp", "pension_gbp")) / (cost - sbc), key=r["source_key"]))
        ukfy = {self.fy(u["end"]): u for u in uk if u["end"].month == c["fy_end_month"]}
        a, b = c["wage_base_fy"], c["wage_last_fy"]
        wage = (ukfy[b]["per"] / ukfy[a]["per"]) ** (1 / (b - a)) - 1
        fm = c["fy_end_month"] + 1
        fx_fy = fx_avg(dt.date(fy - 1, fm, 1), dt.date(fy, c["fy_end_month"], calendar.monthrange(fy, c["fy_end_month"])[1]))
        cph = ukfy[b]["per"] * (1 + wage) * fx_fy
        # ------------------------------------------------------------ margin, debt, EBITDA
        peers = rows(D, "public_comps_sec_xbrl.csv")
        gms = sorted(num(p["ttm_gross_margin"]) for p in peers)
        gm = float(np.median(gms))
        bdc, npl = rows(D, "credit_bdc_10k_positions.csv"), rows(D, "nport_loans.csv")
        last: dict = {}
        for r in npl:
            if r["status"] != "funded":
                continue
            k = r["fund_key"]
            if k not in last or r["report_date"] > last[k][0]["report_date"]:
                last[k] = [r]
            elif r["report_date"] == last[k][0]["report_date"]:
                last[k].append(r)
        np_funded = sum(num(x["par_usd"]) for v in last.values() for x in v)
        bdc_funded = sum(num(r["par_usd"]) for r in bdc)
        debt = bdc_funded + np_funded
        rates = [num(r["interest_rate"]) for r in bdc if r["interest_rate"]]
        loan_rate = sum(rates) / len(rates)
        capacity = self.value(c["capacity_key"])
        ebitda = rev * gm - hc * cph
        interest = debt * loan_rate
        fcf = ebitda - interest
        grid = []
        for g in (gms[0], gm, gms[-1]):
            for s in (c["us_share_band"][0], share, c["us_share_band"][1]):
                e = rev * g - us_avg / s * cph
                grid.append((g, s, us_avg / s, e, e / rev))
        # ------------------------------------------------------------ funding
        fund = collections.defaultdict(lambda: [0.0, 0.0, 0])
        for x in rows(D, "FORMD_offerings.csv"):
            if x["role"] == "company":
                y = self.fy(day(x["date_of_first_sale"]))
                fund[y][1 if x["business_combination"] == "true" else 0] += num(x["amount_sold_usd"])
                fund[y][2] += 1
        # ------------------------------------------------------------ valuation
        px = {r["series"]: num(r["cost_per_share"]) for r in rows(D, "series_prices.csv")}
        pm = {p["month"]: p for p in rows(D, "nport_price_monthly.csv")}
        val, px_l = self.value(c["valuation_key"]), self.value(c["price_key"])
        shares = val / px_l
        px_j, px_k = (px.get(s) for s in c["series_recent"])
        ark = num(pm[max(pm)]["max_price"])                    # the highest fund mark of the latest month
        rd = day(c["round_date"])
        i = max(j for j, q in enumerate(self.me) if q <= rd)
        rr_round = float(rr[i] * (rr[i + 1] / rr[i]) ** ((rd - self.me[i]).days / (self.me[i + 1] - self.me[i]).days))
        mk = pm[max(pm)]
        implied = num(mk["median_price"]) * shares
        # ------------------------------------------------------------ pay, visas, federal
        limits = {int(r["year"]): num(r["elective_deferral_limit_402g"]) for r in rows(D, "irs_402g_limits.csv")}
        k = []
        for r in sorted(rows(D, "dol_5500_sch_h.csv"), key=lambda r: r["SCH_H_PLAN_YEAR_BEGIN_DATE"]):
            y = int(r["SCH_H_PLAN_YEAR_BEGIN_DATE"][:4])
            act = (num(k401[y]["active_boy"]) + num(k401[y]["active_eoy"])) / 2
            pc = num(r["PARTICIPANT_CONTRIB_AMT"])
            k.append(dict(y=y, act=act, pc=pc, per=pc / act, lim=limits.get(y), er=num(r["EMPLR_CONTRIB_INCOME_AMT"]), assets=num(r["NET_ASSETS_EOY_AMT"])))
        h1b = collections.Counter()
        for r in rows(D, "uscis_h1b.csv"):
            h1b[int(r["fiscal_year"]) + 1] += int(r["initial_approval"]) + int(r["continuing_approval"])
        fed = collections.defaultdict(lambda: [0.0, 0])
        for r in rows(D, "fpds_actions.csv"):
            v = num(r["obligated_usd"])
            if v:
                fed[self.fy(day(r["signed_date"]))][0] += v
                fed[self.fy(day(r["signed_date"]))][1] += 1
        # ------------------------------------------------------------ year by year
        g_q = (rr[-1] / rr[0]) ** 0.25 - 1
        fy_next = lambda g: sum(rr[-1] * (1 + g) ** (j / 3) / 12 for j in range(1, 13))      # noqa: E731
        scen = [("Flat", 0.0), ("Half the pace", g_q / 2), ("Same pace", g_q)]
        scen = [(n, g, fy_next(g)) for n, g in scen]
        bdc24 = sum(num(r["par_usd"]) for r in rows(D, "credit_bdc_10k_positions_2024.csv"))
        at = lambda d: sum(num(r["par_usd"]) for r in npl if r["status"] == "funded" and r["report_date"] == d)     # noqa: E731
        dec = {fy - 1: (bdc24, at(f"{fy - 2}-12-31")), fy: (bdc_funded, at(f"{fy - 1}-12-31"))}
        yby = []
        k_by = {r["y"] + 1: r for r in k}
        rrl, gr = self.value(c["rr_last_key"]), self.value(c["growth_key"])
        for y in range(c["first_year"], c["last_year"] + 1):
            cy = y - 1
            u = ukfy.get(y) or next((x for x in uk if x["end"].month != c["fy_end_month"] and self.fy(x["end"]) == y), None)
            mo = f"{y}-{c['fy_end_month']:02d}" if y < c["last_year"] else max(pm)
            d = dec.get(y)
            yby.append(dict(fy=y, revenue=rev if y == fy else None, rr_end=rrl if y == fy else None, rr_bound=rrl / (1 + gr) if y == fy - 1 else None,
                            form_d=fund[y][0] if y in fund else 0.0, form_d_bc=fund[y][1] if y in fund else 0.0, n=fund[y][2] if y in fund else 0,
                            mark=num(pm[mo]["median_price"]) if mo in pm else None, mark_month=mo if mo in pm else None, mark_funds=int(pm[mo]["funds"]) if mo in pm else None,
                            debt_10k=d[0] if d else (0.0 if y < fy - 1 else None), debt_np=d[1] if d else (0.0 if y < fy - 1 else None),
                            health=health.get(cy), k401=int(k401[cy]["active_eoy"]) if cy in k401 else None, global_=round(health[cy] / share) if cy in health else None,
                            uk_staff=u["staff"] if u else None, uk_usd=u["per_usd"] if u else None, deferrals=k_by[y]["pc"] if y in k_by else None,
                            h1b=h1b.get(y), fed=fed[y][0] if y in fed else 0.0, fed_n=fed[y][1] if y in fed else 0))
        return dict(cfg=c, rr=rr, resid=resid, me=self.me, rev=rev, rev_q=rev_q, cases=cases, low=low, high=high, health=health, k401=k401, us_avg=us_avg, hc=hc,
                    uk=uk, ukfy=ukfy, wage=wage, cph=cph, peers=peers, gm=gm, gms=gms, bdc=bdc, npl=npl, np_funded=np_funded, bdc_funded=bdc_funded, debt=debt,
                    loan_rate=loan_rate, capacity=capacity, ebitda=ebitda, interest=interest, fcf=fcf, grid=grid, fund=dict(fund), val=val, px_l=px_l, px_j=px_j, px_k=px_k,
                    ark=ark, shares=shares, rr_round=rr_round, mult_round=val / rr_round, mult_end=val / rr[-1], mark=mk, implied=implied, implied_ark=ark * shares,
                    k=k, yby=yby, scen=scen, g_q=g_q, fy=fy, health_last=health[y1], pm=pm)


def save(m: dict, out: Path) -> None:
    r2 = lambda x: "" if x is None else round(x)                                                   # noqa: E731
    fy, me, rr = m["fy"], m["me"], m["rr"]
    mdl = Model.__new__(Model)
    mdl.c = m["cfg"]
    write(out, "fin_monthly_runrate.csv", ["month_end", "fiscal_year", "fitted_run_rate_usd", "month_revenue_usd", "mom_growth"],
          [[d.isoformat(), f"FY{mdl.fy(d)}", round(rr[i]), round(rr[i] / 12), round(rr[i] / rr[i - 1] - 1, 4) if i else ""] for i, d in enumerate(me)])
    write(out, "fin_revenue_sensitivity.csv", ["case", "fy2026_revenue_usd", "what_changes"], [[a, round(v), w] for a, v, w in m["cases"]])
    write(out, "fin_fiscal_years.csv", ["fiscal_year", "revenue_usd", "revenue_if_rr_quarter_x4_usd", "revenue_low_usd", "revenue_high_usd", "us_avg_participants",
                                        "us_share_scenario", "avg_global_employees_scenario", "revenue_per_employee_usd", "gross_margin", "gross_profit_usd",
                                        "cost_per_employee_usd", "operating_cost_usd", "ebitda_proxy_ex_sbc_usd", "ebitda_margin", "visible_debt_usd", "loan_rate",
                                        "interest_on_visible_debt_usd", "fcf_proxy_usd", "fcf_margin"],
          [[f"FY{fy}", round(m["rev"]), round(m["rev_q"]), round(m["low"]), round(m["high"]), round(m["us_avg"]), m["cfg"]["us_share"], round(m["hc"]), round(m["rev"] / m["hc"]),
            round(m["gm"], 4), round(m["rev"] * m["gm"]), round(m["cph"]), round(m["hc"] * m["cph"]), round(m["ebitda"]), round(m["ebitda"] / m["rev"], 4), round(m["debt"]),
            round(m["loan_rate"], 4), round(m["interest"]), round(m["fcf"]), round(m["fcf"] / m["rev"], 4)]])
    write(out, "fin_sensitivity_fy2026.csv", ["gross_margin", "us_share", "avg_global_employees", "ebitda_proxy_usd", "ebitda_margin"],
          [[round(g, 4), s, round(h), round(e), round(x, 4)] for g, s, h, e, x in m["grid"]])
    s = m["cfg"]["us_share_band"]
    write(out, "fin_headcount.csv", ["year_end", "us_hw_plan_501_active", "us_401k_plan_001_active", "global_if_us_55pct", "global_if_us_65pct", "global_if_us_75pct", "uk_avg_staff_fy_ending_jan_next"],
          [[y, v, m["k401"][y]["active_eoy"] if y in m["k401"] else "", round(v / s[0]), round(v / m["cfg"]["us_share"]), round(v / s[1]),
            next((u["staff"] for u in m["uk"] if u["end"] == dt.date(y + 1, 1, 31)), "")] for y, v in sorted(m["health"].items())])
    write(out, "fin_uk_cost_per_employee.csv", ["period_end", "months", "turnover_gbp", "total_cost_gbp", "share_based_pay_gbp", "cost_ex_sbc_gbp", "avg_staff", "cost_per_employee_gbp_annualised",
                                                "usd_per_gbp", "cost_per_employee_usd", "markup_on_cost", "personnel_share_of_cost", "source_key"],
          [[u["end"], u["months"], round(u["turnover"]), round(u["cost"]), round(u["sbc"]), round(u["ex"]), u["staff"], round(u["per"]), round(u["fx"], 4) if u["fx"] else "",
            r2(u["per_usd"]), round(u["markup"], 4), round(u["people"], 4), u["key"]] for u in m["uk"]])
    write(out, "fin_funding_by_fiscal_year.csv", ["fiscal_year", "form_d_equity_non_bc_usd", "form_d_business_combination_usd", "offerings"],
          [[f"FY{y}", round(v[0]), round(v[1]), v[2]] for y, v in sorted(m["fund"].items())])
    write(out, "fin_401k_contributions.csv", ["plan_year", "avg_active_participants", "participant_contributions_usd", "per_participant_usd", "irs_402g_limit_usd",
                                              "per_participant_share_of_limit", "employer_contributions_usd", "net_assets_eoy_usd"],
          [[r["y"], round(r["act"]), round(r["pc"]), round(r["per"]), r2(r["lim"]), round(r["per"] / r["lim"], 4) if r["lim"] else "", round(r["er"]), round(r["assets"])] for r in m["k"]])
    q = []
    for d in me:
        if mdl.fy(d) == fy and (d.month - m["cfg"]["fy_end_month"]) % 3 == 0:
            i = me.index(d)
            q.append([d.isoformat(), f"FY{fy}", f"Q{len(q) + 1}", round(rr[i]), round(sum(rr[j] / 12 for j in range(i - 2, i + 1))), round(rr[i] / 4)])
    write(out, "fin_quarterly_runrate.csv", ["quarter_end", "fiscal_year", "fiscal_quarter", "fitted_run_rate_usd", "quarter_revenue_usd", "quarter_revenue_if_rr_div4_usd"], q)
    write(out, "fin_ttm_jan2026.csv", ["window", "revenue_usd", "run_rate_at_end_usd", "us_avg_participants", "global_employees_at_end_scenario", "avg_global_employees_scenario",
                                       "cost_per_employee_usd", "gross_margin", "ebitda_proxy_ex_sbc_usd", "ebitda_margin"],
          [[f"FY{fy}", round(m["rev"]), round(rr[-1]), round(m["us_avg"]), round(m["health_last"] / m["cfg"]["us_share"]), round(m["hc"]), round(m["cph"]), round(m["gm"], 4), round(m["ebitda"]), round(m["ebitda"] / m["rev"], 4)]])
    mk = m["mark"]
    write(out, "fin_valuation.csv", ["item", "date", "value", "unit", "basis"],
          [["Series J price per share", "", m["px_j"], "USD", ""], ["Series K price per share", "", m["px_k"], "USD", ""], ["Series L price per share", "", m["px_l"], "USD", ""],
           ["Series L valuation", "", m["val"], "USD", ""], ["Implied fully diluted shares (model)", "", round(m["shares"]), "shares", ""], ["Series L valuation ÷ fitted run-rate (model)", "", round(m["mult_round"], 2), "x", ""],
           ["Series L valuation ÷ Jan 2026 run-rate (model)", "", round(m["mult_end"], 2), "x", ""], ["Median N-PORT mark, latest month", mk["month"], num(mk["median_price"]), "USD per share", ""],
           ["Implied equity value at latest median mark (model)", mk["month"], round(m["implied"]), "USD", ""], ["ARK fair value per share", "", m["ark"], "USD", ""], ["Implied equity value at ARK mark (model)", "", round(m["implied_ark"]), "USD", ""]])
    write(out, "fin_year_by_year.csv", ["fiscal_year", "revenue_usd_model", "run_rate_at_fy_end_usd_stated", "run_rate_at_fy_end_upper_bound_usd", "form_d_financing_sold_usd",
                                        "form_d_business_combination_sold_usd", "form_d_offerings", "median_nport_mark_fy_end_usd", "nport_mark_month", "nport_mark_funds",
                                        "bdc_10k_funded_par_31dec_usd", "nport_funded_par_31dec_usd", "visible_funded_par_31dec_usd", "us_hw_plan_501_participants_31dec",
                                        "us_401k_plan_001_active_31dec", "global_employees_at_65pct_scenario", "uk_avg_staff", "uk_cost_per_employee_usd", "401k_employee_deferrals_usd",
                                        "h1b_approvals_initial_plus_continuing", "federal_obligations_fpds_usd", "federal_actions_fpds_nonzero"],
          [[f"FY{r['fy']}", r2(r["revenue"]), r2(r["rr_end"]), r2(r["rr_bound"]), round(r["form_d"]), round(r["form_d_bc"]), r["n"], r["mark"] if r["mark"] else "", r["mark_month"] or "",
            r["mark_funds"] or "", r2(r["debt_10k"]), r2(r["debt_np"]), "" if r["debt_10k"] is None else round(r["debt_10k"] + r["debt_np"]), r["health"] or "", r["k401"] or "",
            r["global_"] or "", r["uk_staff"] or "", r2(r["uk_usd"]), r2(r["deferrals"]), "" if r["h1b"] is None else r["h1b"], round(r["fed"]), r["fed_n"]] for r in m["yby"]]
          + [[f"FY{m['cfg']['last_year']} ({n}, scenario)", round(v)] + [""] * 20 for n, g, v in m["scen"]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = Model(Path(a.data), json.load(open(a.config, encoding="utf-8"))).run()
    save(res, Path(a.out))
    print(f"revenue {res['rev'] / 1e9:.3f}B, EBITDA {res['ebitda'] / 1e9:.3f}B, FCF {res['fcf'] / 1e9:.3f}B, debt {res['debt'] / 1e9:.3f}B, cost per employee {res['cph']:,.0f}")
