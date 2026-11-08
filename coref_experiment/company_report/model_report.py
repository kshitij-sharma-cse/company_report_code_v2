"""Report sections for the financial, credit and market views. Every figure is read from the model results; the text holds no number of its own."""
from __future__ import annotations

import csv
import html
from pathlib import Path

_e = lambda x: html.escape(str(x))                                                                  # noqa: E731


def usd(v, d=2):
    if v in (None, ""):
        return "—"
    v = float(v)
    s, a = ("−" if v < 0 else ""), abs(v)
    if a >= 1e9:
        return f"{s}${a / 1e9:,.{d}f}B"
    if a >= 1e6:
        return f"{s}${a / 1e6:,.1f}M"
    if a >= 1e3:
        return f"{s}${a / 1e3:,.0f}K"
    return f"{s}${a:,.0f}"


def pct(v, d=1):
    return "—" if v in (None, "") else f"{float(v) * 100:.{d}f}%".replace("-", "−")


def pdp(v):
    if v in (None, ""):
        return "—"
    v = float(v)
    return "<0.0001%" if v < 1e-6 else f"{v * 100:.4f}%" if v < 1e-4 else f"{v * 100:.2f}%"


def xx(v):
    return "n/m" if v in (None, "", "n/m") else f"{float(v):.2f}x"


def table(w, head, body, num_from=1):
    w("<table><tr>" + "".join(f"<th{' class=n' if i >= num_from else ''}>{_e(h)}</th>" for i, h in enumerate(head)) + "</tr>" +
      "".join("<tr>" + "".join(f"<td{' class=n' if i >= num_from else ''}>{_e(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in body) + "</table>")


def rows(folder: Path, name: str) -> list[dict]:
    with open(folder / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def financial_section(w, f: dict, data: Path, svg, short: str) -> None:
    c = f["cfg"]
    D = data / "data"
    fy = f["fy"]
    fig = {r["key"]: r for r in rows(D, "company_figures.csv")}
    w(f'<div class="page"></div><h2>Financial model</h2><div class="box">Model figures, not company accounts. {_e(short)} publishes no financial statements. The inputs are public records and the assumptions are '
      f'listed in model_config. Global staff uses a {pct(c["us_share"], 0)} U.S. share, and gross margin is the median of listed peers. Fiscal years end in month {c["fy_end_month"]}.</div>')
    w("<h3>Revenue statements in filings</h3>")
    table(w, ["Metric", "As of", "Value", "Filer", "Form"], [[fig[k]["metric"], fig[k]["as_of"], usd(fig[k]["value"], 1), fig[k]["filer"], fig[k]["form"]] for k in (c["rr_first_key"], c["rr_last_key"])] +
          [[fig[c["growth_key"]]["metric"], fig[c["growth_key"]]["as_of"], pct(fig[c["growth_key"]]["value"], 0) + " or more", fig[c["growth_key"]]["filer"], fig[c["growth_key"]]["form"]]], num_from=9)
    w(f"<h3>FY{fy} revenue (model): {usd(f['rev'])}</h3>")
    w(svg([{"holder": "Fitted run-rate", "kind": "fair value", "date": d, "value": float(v) / 1e9} for d, v in zip(f["me"], f["rr"])], "Fitted monthly revenue run-rate, $B"))
    table(w, ["Case", f"FY{fy} revenue", "vs base"], [[a, usd(v), pct(v / f["rev"] - 1)] for a, v, _ in f["cases"]])
    rr = {d: float(v) for d, v in zip(f["me"], f["rr"])}
    qs = [d for d in f["me"][1:] if (d.month - c["fy_end_month"]) % 3 == 0]
    w("<h3>Quarterly revenue (model)</h3>")
    table(w, ["Quarter end", "Revenue", "If run-rate = quarter x 4"], [[d, usd(sum(float(f["rr"][j]) / 12 for j in range(f["me"].index(d) - 2, f["me"].index(d) + 1))), usd(rr[d] / 4)] for d in qs])
    w("<h3>Staff</h3>")
    b = c["us_share_band"]
    table(w, ["Year end", "Health plan", "401(k)", f"Global at {pct(b[0], 0)}", f"Global at {pct(c['us_share'], 0)}", f"Global at {pct(b[1], 0)}"],
          [[y, f"{v:,}", f["k401"][y]["active_eoy"] if y in f["k401"] else "—", f"{round(v / b[0]):,}", f"{round(v / c['us_share']):,}", f"{round(v / b[1]):,}"] for y, v in sorted(f["health"].items())])
    w("<h3>Cost per employee (U.K. subsidiary accounts)</h3>")
    table(w, ["Year to", "Turnover", "Staff pay", "Cost ex-SBC", "Staff", "£ per employee", "USD/GBP", "$ per employee", "Mark-up"],
          [[u["end"], f"£{u['turnover'] / 1e6:,.2f}M", f"£{u['pay'] / 1e6:,.2f}M", f"£{u['ex'] / 1e6:,.2f}M", u["staff"], f"£{u['per'] / 1e3:,.1f}K", f"{u['fx']:.4f}" if u["fx"] else "—", usd(u["per_usd"]), pct(u["markup"], 2)] for u in f["uk"]])
    w(f'<p class="note">FY{fy} cost per employee is {usd(f["cph"])}: the latest U.K. figure, grown {pct(f["wage"], 2)}, at the average Treasury rate.</p>')
    last = f["uk"][-1]
    w(f'<p class="note">Share-based pay in the U.K. accounts to {last["end"]}: £{last["sbc"] / 1e6:.1f}M.</p>')
    w("<h3>EBITDA and FCF proxies</h3>")
    table(w, ["Item", "Value"], [[f"Revenue FY{fy}", usd(f["rev"])], ["Gross margin (peer median)", pct(f["gm"], 2)], ["Average global staff", f"{round(f['hc']):,}"], ["Operating cost", usd(f["hc"] * f["cph"])],
                                 ["EBITDA proxy", f"{usd(f['ebitda'])} ({pct(f['ebitda'] / f['rev'])})"], ["Visible funded debt", usd(f["debt"])], ["Loan rate", pct(f["loan_rate"], 3)], ["Interest", usd(f["interest"])], ["FCF proxy", usd(f["fcf"])]])
    table(w, ["Gross margin", "U.S. share", "Avg global staff", "EBITDA proxy", "Margin"], [[pct(g), pct(s, 0), f"{round(h):,}", usd(e), pct(x)] for g, s, h, e, x in f["grid"]], num_from=2)
    w("<h3>Listed peers</h3>")
    pc = {r["company"]: r for r in rows(D, "peer_credit_xbrl.csv")}
    xm = lambda v: f"{float(v):.1f}x" if v else "—"                                                 # noqa: E731
    table(w, ["Peer", "TTM to", "TTM revenue", "Gross margin", "Public float", "Float / revenue", "EV / revenue"],
          [[p["company"], p["fiscal_q_end"], usd(p["ttm_rev_usd"]), pct(p["ttm_gross_margin"]), usd(pc.get(p["company"], {}).get("public_float_usd")), xm(pc.get(p["company"], {}).get("float_to_ttm_rev")), xm(pc.get(p["company"], {}).get("float_ev_to_ttm_rev"))] for p in f["peers"]])
    table(w, ["Peer", "Latest quarter x 4", "Growth on a year earlier"], [[k, usd(v["latest_quarter_x4_usd"]), pct(v["latest_quarter_yoy"], 0)] for k, v in pc.items()])
    w("<h3>Funding by fiscal year (Form D)</h3>")
    fund = sorted(f["fund"].items())
    table(w, ["FY", "Financing", "Business combination", "Offerings"], [[f"FY{y}", usd(v[0]), usd(v[1]), v[2]] for y, v in fund] +
          [["Total", usd(sum(v[0] for _, v in fund)), usd(sum(v[1] for _, v in fund)), sum(v[2] for _, v in fund)]])
    w("<h3>Valuation</h3>")
    table(w, ["Item", "Value"], [*[[f"Series {s} cost per share", f"${v:.2f}"] for s, v in zip(c["series_recent"], (f["px_j"], f["px_k"])) if v], ["Latest round price", f"${f['px_l']:.2f}"], ["Latest round valuation", usd(f["val"], 0)],
                                 ["Implied shares (model)", f"{round(f['shares']):,}"], ["Valuation / run-rate at the round (model)", f"{f['mult_round']:.2f}x"], ["Valuation / fitted run-rate at fiscal year end (model)", f"{f['mult_end']:.2f}x"],
                                 [f"Median N-PORT mark, {f['mark']['month']}", f"${float(f['mark']['median_price']):.2f}"], ["Implied value at that mark (model)", usd(f["implied"], 1)], ["Highest fund mark, latest month", f"${f['ark']:.2f}"],
                                 ["Implied value at that highest mark (model)", usd(f["implied_ark"], 1)]])
    w("<h3>Revenue multiples used by funds (Level 3 disclosures)</h3>")
    table(w, ["Date", "Filer", "Low", "High", "Central"], [[r["valuation_date"], r["filer"], f"{r['low']}x", f"{r['high']}x", f"{r['central']}x" if r["central"] else "—"] for r in rows(D, "revenue_multiples.csv")], num_from=2)
    w("<h3>Monthly fund marks (Form N-PORT, last 12 months)</h3>")
    pm = rows(D, "nport_price_monthly.csv")[-12:]
    table(w, ["Month", "Funds", "Median", "Low", "High"], [[p["month"], p["funds"], f"${float(p['median_price']):.2f}", f"${float(p['min_price']):.2f}", f"${float(p['max_price']):.2f}"] for p in pm])
    w("<h3>Year by year (fiscal years)</h3>")
    table(w, ["FY", "Revenue", "Form D", "Bus. comb.", "Visible debt", "Health plan", "Global", "U.K. staff", "U.K. $/emp", "401(k)", "H-1B", "Federal"],
          [[f"FY{r['fy']}", usd(r["revenue"]), usd(r["form_d"]), usd(r["form_d_bc"]), "—" if r["debt_10k"] is None else usd(r["debt_10k"] + r["debt_np"]), r["health"] or "—", r["global_"] or "—", r["uk_staff"] or "—",
            usd(r["uk_usd"]), usd(r["deferrals"]), "—" if r["h1b"] is None else r["h1b"], usd(r["fed"])] for r in f["yby"]])
    w(f"<h3>FY{c['last_year']} revenue scenarios (illustrative)</h3>")
    table(w, ["Scenario", "Quarterly growth", "Revenue"], [[n, pct(g, 2), usd(v)] for n, g, v in f["scen"]])
    fed = rows(D, "fpds_actions.csv")
    sec = [r for r in rows(D, "uspto_assignments.csv") if r["conveyance"] == "SECURITY INTEREST"]
    w("<h3>Other government records</h3>")
    table(w, ["Record", "Value"], [["Federal contract actions (FPDS)", f"{len(fed)} actions, {usd(sum(float(r['obligated_usd'] or 0) for r in fed))} obligated"],
                                   ["Patent security interests (USPTO)", "; ".join(f"{r['execution_date']} to {r['assignees']}" for r in sec[:4])],
                                   [f"{rows(D, 'fr_subsidiary.csv')[0]['entity']} (France), {rows(D, 'fr_subsidiary.csv')[0]['api_year_label']}", f"turnover €{float(rows(D, 'fr_subsidiary.csv')[0]['turnover_eur']) / 1e6:.2f}M"]], num_from=9)


def credit_section(w, f: dict, r: dict, data: Path, out: Path, short: str) -> None:
    D = data / "data"
    b = r["base"]
    w(f'<div class="page"></div><h2>Credit model</h2><div class="box">Model output: {_e(short)} has no public rating, and this is not a rating from any agency. It inherits the assumptions of the financial model. '
      'The business-profile scores come from rule tables in model_config, read off measured values.</div>')
    p = r["pds"]
    table(w, ["Item", "Value"], [["Synthetic rating", r["rating"]], ["Final notch", f"{b['final']:.2f}"], ["1-year PD", pct(p[0]["r"], 2)], ["5-year PD", pct(p[2]["r"], 2)], ["Monte Carlo 5-year PD", pct(r["mc"][1][7], 2)], ["Modelled LGD", pct(r["lgd_m"], 1)]])
    w("<h3>Debt and loan market</h3>")
    table(w, ["Item", "Value"], [[f"Term loans in {r['n_bdc']} lender 10-Ks", usd(r["bdc_par"])], ["Unfunded commitments in those 10-Ks", usd(r["unfunded"])], ["Visible funded debt, with N-PORT", usd(r["debt"])], ["Largest monthly last-out par", usd(r["lastout"])],
                                 ["Quarter-end loan prices, % of par", f"{r['price_lo']:.1f} to {r['price_hi']:.1f}"], ["N-PORT default flags", f"{r['n_default']} of {r['n_loans']} rows"], ["N-PORT arrears flags", f"{r['n_arrears']} of {r['n_loans']} rows"], ["Par-weighted years to maturity", f"{r['m_years']:.2f}"]])
    table(w, ["Lender", "Instrument", "Rate", "Maturity", "Par", "Fair value"], [[a["fund"], a["instrument"][:40], pct(a["interest_rate"], 2), a["maturity"], usd(a["par_usd"]), usd(a["fair_value_usd"])] for a in f["bdc"]], num_from=2)
    prior = next(y for y in f["yby"] if y["fy"] == f["fy"] - 1)
    w(f'<p class="note">Prior-year columns of the same 10-Ks show {usd(prior["debt_10k"])} funded a year earlier.</p>')
    table(w, ["Quarter", "Price", "SOFR", "Coupon − SOFR", "Years", "Discount margin"], [[q["month"], f"{q['vw']:.3f}", pct(q["sofr"], 2), pct(q["gap"], 2), f"{q['years']:.2f}", pct(q["dm"], 3)] for q in r["qm"]])
    w("<h3>Credit metrics</h3>")
    table(w, ["Basis", "Revenue", "EBITDA", "Debt", "Interest", "Debt/EBITDA", "EBITDA/int.", "FCF/debt", "Rating"], [[x["name"], usd(x["rev"]), usd(x["ebitda"]), usd(x["debt"]), usd(x["interest"]), xx(x["lev"]), xx(x["cov"]), pct(x["fcf_debt"]), x["rating"]] for x in r["bases"]])
    w("<h3>Business profile</h3>")
    table(w, ["Sub-factor", "Measured value", "Notch"], [[x["sub_factor"].replace("_", " "), x["metric"], x["notch"]] for x in rows(out, "credit_business_profile.csv")])
    w("<h3>Scorecard, primary basis</h3>")
    table(w, ["Factor", "Weight", "Notch", "Weighted"], [[a["factor"], pct(a["weight"], 0) if a["weight"] else "", a["factor_notch"], a["weighted_notch"]] for a in rows(out, "credit_scorecard.csv") if a["basis"] == b["name"]])
    table(w, ["Peer", "TTM revenue", "EBITDA ex-SBC", "Debt", "Debt/EBITDA", "EBITDA/interest"], [[a["company"], usd(a["ttm_revenue_usd"]), usd(a["ttm_ebitda_ex_sbc_usd"]), usd(a["debt_usd"]), xx(a["debt_to_ebitda"]), xx(a["ebitda_to_interest"])] for a in rows(D, "peer_credit_xbrl.csv")])
    table(w, ["Variant", "Final notch", "Rating", "1-year PD"], [[a["variant"], a["final_notch"], a["synthetic_rating"], pdp(a["pd_1y"])] for a in rows(out, "credit_scorecard_sensitivity.csv")])
    table(w, ["Gross margin", "U.S. share", "Final notch", "Rating"], [[pct(a["gross_margin"]), pct(a["us_share"], 0), a["final_notch"], a["synthetic_rating"]] for a in rows(out, "credit_rating_grid.csv")], num_from=2)
    w("<h3>Default risk by method</h3>")
    table(w, ["Horizon", "Rating-implied", "Merton", "Merton, 75% vol, capacity drawn", "Market upper bound"], [[f"{x['h']} yr", pdp(x["r"]), pdp(x["m"]), pdp(x["m75"]), pdp(x["mk"])] for x in p])
    mkh = [k.replace("markov_pd_", "Migration, ").replace("constant_hazard_pd_", "Hazard, ") for k in rows(out, "credit_migration_markov.csv")[0] if k != "years"]
    table(w, ["Years", *mkh], [[a.pop("years"), *[pdp(v) for v in a.values()]] for a in rows(out, "credit_migration_markov.csv") if a["years"] in ("1", "3", "5", "10")])
    w("<h3>Expected loss and capital</h3>")
    table(w, ["Rating", "PD", "LGD", "K", "Risk weight", "EL a year", "Lifetime EL"], [[a["rating"], pdp(a["pd_1y_floored"]), pct(a["lgd"], 0), pct(a["capital_K"], 2), pct(a["risk_weight"]), usd(a["expected_loss_1y_usd"]), usd(a["lifetime_expected_loss_usd"])] for a in rows(out, "credit_el_capital.csv")])
    table(w, ["CECL basis", "LGD", "12-month ECL", "Lifetime ECL"], [[a["lgd_basis"], pct(a["lgd"]), usd(a["ecl_12m_usd"]), usd(a["ecl_lifetime_usd"])] for a in rows(out, "credit_cecl.csv")])
    table(w, ["Pricing basis", "EL rate", "Capital charge", "Required spread", "Observed DM", "Not explained"], [[a["lgd_basis"], pct(a["expected_loss_rate"], 2), pct(a["capital_charge"], 2), pct(a["required_spread"], 2), pct(a["observed_discount_margin"], 2), pct(a["spread_not_explained_by_credit_risk"], 2)] for a in rows(out, "credit_pricing.csv")])
    table(w, ["Lowest grade kept", "Max funded debt", "Headroom", "Min EBITDA", "Cushion"], [[a["lowest_grade_kept"], a["max_funded_debt_usd"] if a["max_funded_debt_usd"].startswith("already") else usd(a["max_funded_debt_usd"]), usd(a["headroom_over_current_debt_usd"]), usd(a["min_ebitda_usd"]), pct(a["ebitda_cushion_pct"])] for a in rows(out, "credit_debt_capacity.csv")])
    w("<h3>Stress tests</h3>")
    labels = {"S0": "Base", "S1": "Rates up", "S2": "Capacity drawn", "S3": "Lowest peer gross margin", "S4": "Growth stall", "S5": "Equity value cut", "S6": "Severe combined", "S7": "Unseen debt"}
    table(w, ["ID", "Scenario", "EBITDA", "Debt", "Notch", "Change", "Rating", "1-year PD"], [[a["id"], labels.get(a["id"], ""), usd(a["ebitda_proxy_usd"]), usd(a["debt_usd"]), a["scorecard_notch"], a["notch_change_vs_base"], a["synthetic_rating"], pdp(a["pd_1y_rating_implied"])] for a in rows(out, "credit_stress_tests.csv")], num_from=2)
    w("<h3>Projections</h3>")
    table(w, ["Scenario", "FY", "Growth", "Revenue", "EBITDA", "FCF", "Debt", "Rating"], [[a["scenario"], a["fiscal_year"], pct(a["revenue_growth"]), usd(a["revenue_usd"]), usd(a["ebitda_proxy_usd"]), usd(a["fcf_usd"]), usd(a["debt_usd"]), a["synthetic_rating"]] for a in rows(out, "credit_projections.csv")], num_from=2)
    w("<h3>Monte Carlo</h3>")
    table(w, ["Growth shock", "Paths", "Default by FY2027", "By FY2029", "By FY2031", "EBITDA negative in any year"], [[pct(a["sigma"], 0), f"{int(a['paths']):,}", pct(a["cum_default_FY2027"], 2), pct(a["cum_default_FY2029"], 2), pct(a["cum_default_FY2031"], 2), pct(a["share_paths_ebitda_negative_any_year"], 1)] for a in rows(out, "credit_monte_carlo.csv")])


def market_section(w, f: dict, r: dict, data: Path, short: str) -> None:
    D = data / "data"
    fig = {x["key"]: x for x in rows(D, "company_figures.csv")}
    h = rows(D, "nport_holders.csv")
    w(f'<div class="page"></div><h2>Market view</h2>')
    fed = rows(D, "fpds_actions.csv")
    fr = rows(D, "fedramp.csv")
    us = [x for x in rows(D, "usaspending_awards.csv") if x["search_basis"] == "keyword"]
    w("<h3>At a glance</h3>")
    table(w, ["Metric", "Value", "Basis"], [
        ["Revenue run-rate", usd(fig["rr_last"]["value"], 1), f"{fig['rr_last']['form']}, {fig['rr_last']['filer']}"], ["Run-rate growth", f"more than {pct(fig['growth']['value'], 0)}", fig["growth"]["form"]],
        *([["AI products run-rate", "more than " + usd(fig["ai_rr"]["value"], 1), fig["ai_rr"]["form"]]] if "ai_rr" in fig else []),
        [f"Revenue, FY{f['fy']}", f"{usd(f['rev'])} (range {usd(f['low'])} to {usd(f['high'])})", "model"], ["Valuation", usd(f["val"], 0), fig["valuation"]["form"]], ["Latest round price", f"${f['px_l']:.0f} a share", fig["price"]["form"]],
        ["Fund exposure, latest reports", f"{usd(sum(float(x['value_usd']) for x in h))} across {len(h)} funds", "Form N-PORT"], ["Debt visible", f"{usd(f['debt'])} funded; about {usd(f['capacity'], 0)} more capacity", "10-K, N-PORT, N-CSRS"], ["Credit", f"synthetic {r['rating']}; 1-year PD {pct(r['pds'][0]['r'], 2)}", "model"],
        ["U.S. health plan", f"{f['health_last']:,} active participants", "Form 5500"], [f"Federal obligations (FPDS)", f"{usd(sum(float(x['obligated_usd'] or 0) for x in fed))} on {len(fed)} actions", "FPDS"], ["Federal awards naming the company", f"{usd(sum(float(x['award_amount_usd'] or 0) for x in us), 1)} on {len(us)} awards", "USAspending"],
        ["FedRAMP", f"{len(fr)} authorised offerings, {sum(x['impact_level'] == 'High' for x in fr)} at High", "FedRAMP"]], num_from=9)
    w("<h3>The product in government records</h3>")
    table(w, ["FedRAMP offering", "Impact", "Authorised", "Agency reuse", "Agencies"], [[x["offering"], x["impact_level"], x["authorized"], x["reuse"], x["agencies"][:120]] for x in fr], num_from=9)
    table(w, ["Company", "What its 10-K says"], [[x["company"], x["sentence"][:220]] for x in rows(D, "company_mentions_10k.csv")], num_from=9)
    for x in rows(D, "nsf_awards.csv"):
        w(f'<p class="note">NSF award {_e(x["award"])}, {_e(x["title"])}, {_e(x["awardee"])}, {_e(x["start"])} to {_e(x["end"])}: {usd(x["obligated_usd"])} obligated.</p>')
    pat = [x for x in rows(D, "uspto_assignments.csv") if "ASSIGNMENT" in x["conveyance"].upper() and "CORRECT" not in x["conveyance"].upper() and short.upper() in x["assignees"].upper() and x["execution_date"]]
    years: dict = {}
    for x in pat:
        years[x["execution_date"][-4:]] = years.get(x["execution_date"][-4:], 0) + 1
    table(w, ["Year", "Patent assignments to the company"], [[y, n] for y, n in sorted(years.items())])
    w("<h3>Company statements in filings</h3>")
    table(w, ["Metric", "Words in the filing", "Filer", "Filed"], [[x["metric"], x["quote"], x["filer"], x["filed"]] for x in fig.values()], num_from=9)
    w("<h3>Acquisitions in the filings</h3>")
    table(w, ["Date", "Statement", "Words in the filing"], [[x["as_of"], x["statement"], x["quote"][:160]] for x in rows(D, "acquisitions.csv")], num_from=9)
    w("<h3>Prices paid in fund filings</h3>")
    lots = sorted(rows(D, "dated_lots.csv"), key=lambda x: (-int(x["rows"]), x["acquired"]))[:24]
    table(w, ["Acquired", "Series", "Per share, split-adjusted", "Shares", "Cost", "Holder"], [[x["acquired"], x["series"], f"${float(x['cost_per_share_split_adj']):.2f}", f"{float(x['shares']):,.0f}", usd(x["cost_usd"]), x["holder"][:40]] for x in sorted(lots, key=lambda x: x["acquired"])], num_from=2)
    w("<h3>Largest current holders (Form N-PORT)</h3>")
    table(w, ["House", "Fund", "Report date", "Value", "% NAV", "Exposure"], [[x["fund_house"], x["fund"][:44], x["report_date"], usd(x["value_usd"]), f"{float(x['pct_net_assets']):.2f}%", x["exposure"]] for x in h[:15]], num_from=3)
    w("<h3>Holdings through vehicles</h3>")
    table(w, ["House", "Fund", "Value through vehicles", "Report date"], [[x["fund_house"], x["fund"][:44], usd(x["other_usd"]), x["report_date"]] for x in sorted(h, key=lambda x: -float(x["other_usd"]))[:8] if float(x["other_usd"]) > 0], num_from=2)
    w("<h3>Most concentrated holders</h3>")
    table(w, ["House", "Fund", "Value", "% NAV", "Report date"], [[x["fund_house"], x["fund"][:44], usd(x["value_usd"]), f"{float(x['pct_net_assets']):.2f}%", x["report_date"]] for x in sorted(h, key=lambda x: -float(x["pct_net_assets"]))[:8]], num_from=2)
    lo: dict = {}
    for x in rows(D, "nport_loans.csv"):
        if x["report_date"] >= "2026-01-01":
            e = lo.setdefault(x["fund_house"] or x["series_name"], {})
            e[x["series_name"]] = max(e.get(x["series_name"], ("", 0.0)), (x["report_date"], float(x["par_usd"] or 0) if x["status"] == "funded" else 0.0))
    w("<h3>Funded loans by fund house (latest N-PORT rows, 2026)</h3>")
    table(w, ["House", "Funds", "Largest funded par in a fund"], [[k, len(v), usd(max(p for _, p in v.values()))] for k, v in sorted(lo.items(), key=lambda t: -max(p for _, p in t[1].values()))[:12]])
    fl = rows(D, "nport_flows.csv")
    w(f'<h3>Who is buying and selling</h3><p class="note">Net value of shares added less shares cut each quarter, at the funds own marks. Total {usd(sum(float(x["net_buying_usd"]) for x in fl))}. Fund mergers and share-class moves look like trades.</p>')
    table(w, ["Quarter", "Net buying"], [[x["quarter"], usd(x["net_buying_usd"])] for x in sorted(fl, key=lambda x: -float(x["net_buying_usd"]))[:6]])
    w("<h3>Price levels</h3>")
    table(w, ["Mark (split-adjusted)", "First month", "Last month", "Months as most common mark"], [[f"${float(x['price']):.2f}", x["first_month"], x["last_month"], x["months"]] for x in rows(D, "price_levels.csv")])
    wp = data / "data" / "welfare_plans.csv"
    if wp.exists():
        table(w, ["Plan year", "Severance plan, active at year end"], [[x["plan_year"], x["active_participants_eoy"]] for x in rows(D, "welfare_plans.csv") if x["plan_kind"] == "Severance"])
    w("<h3>Inside the 401(k) plan</h3>")
    table(w, ["Plan year", "Average active", "Employee deferrals", "Per participant", "IRS limit", "Employer contributions", "Net assets"], [[x["y"], f"{round(x['act']):,}", usd(x["pc"]), usd(x["per"]), usd(x["lim"]), usd(x["er"]), usd(x["assets"])] for x in f["k"]])
    ag: dict = {}
    for x in fed:
        if float(x["obligated_usd"] or 0):
            ag[x["kind"]] = ag.get(x["kind"], 0) + float(x["obligated_usd"])
    w("<h3>The U.S. government as a customer</h3>")
    table(w, ["Award", "Agency", "Start", "Amount", "Description"], [[x["award_id"], x["agency"], x["start"], usd(x["award_amount_usd"]), x["description"][:70]] for x in sorted(us, key=lambda x: -float(x["award_amount_usd"] or 0))[:10]], num_from=9)


def records_section(w, D: Path) -> None:
    """Whatever public-record tables this company has. A table with no source is left out."""
    have = lambda n: (D / n).exists() and len(rows(D, n)) > 0                                       # noqa: E731
    w('<div class="page"></div><h2>Public records</h2><div class="box">Tables read by script from the source files. No model estimates are made for this company, because its filings give no revenue or lender data.</div>')
    if have("formd_notices.csv"):
        nt = rows(D, "formd_notices.csv")
        tot = {k: sum(float(x["amount_sold"] or 0) for x in nt if x["kind"] == k) for k in ("equity", "debt")}
        w(f'<h3>Form D notices, raw filings</h3><p class="note">The latest notice of each offering, read from the filings. Equity sold {usd(tot["equity"], 1)}; debt sold {usd(tot["debt"], 1)}. This set can run later than the Form D database used on the first pages.</p>')
        table(w, ["Latest notice", "Signed", "First sale", "Kind", "Offering", "Sold", "Investors"], [[x["latest_notice"], x["signed"], x["first_sale"], x["kind"], usd(x["offering_amount"], 1), usd(x["amount_sold"], 1), x["investors"]] for x in nt], num_from=9)
    if have("uscis_h1b.csv"):
        w("<h3>H-1B approvals</h3>")
        table(w, ["Fiscal year", "Employer", "Initial", "Continuing"], [[x["fiscal_year"], x["employer"], x["initial_approval"], x["continuing_approval"]] for x in rows(D, "uscis_h1b.csv")], num_from=2)
    if have("fda_pma.csv"):
        w("<h3>FDA approvals</h3>")
        table(w, ["Number", "Supplement", "Decision date", "Decision", "Device", "Type", "Reason"], [list(x.values()) for x in rows(D, "fda_pma.csv")], num_from=9)
    if have("fda_udi.csv"):
        w("<h3>Devices listed with FDA</h3>")
        table(w, ["Brand", "Model", "Company", "Published"], [list(x.values()) for x in rows(D, "fda_udi.csv")], num_from=9)
    if have("clinical_trials.csv"):
        w("<h3>Studies on ClinicalTrials.gov</h3>")
        table(w, ["NCT", "Title", "Sponsor", "Status", "Start", "Enrollment"], [[x["nct"], x["title"][:80], x["sponsor"], x["status"], x["start"], x["enrollment"]] for x in rows(D, "clinical_trials.csv")], num_from=9)
    if have("federal_awards.csv"):
        w("<h3>Federal awards (USAspending)</h3>")
        table(w, ["Award", "Agency", "Type", "Start", "End", "Amount", "Description"], [[x["award_id"], x["agency"], x["type"], x["start"], x["end"], usd(x["amount_usd"]), x["description"][:70]] for x in rows(D, "federal_awards.csv")], num_from=9)
    if have("ppp_loans.csv"):
        w("<h3>PPP loan</h3>")
        table(w, ["Approved", "Borrower", "Initial", "Final", "Jobs", "Forgiven", "Lender"], [[x["date_approved"], x["borrower"], usd(x["initial_amount"]), usd(x["final_amount"]), x["jobs"], usd(x["forgiven"]), x["lender"]] for x in rows(D, "ppp_loans.csv")], num_from=9)
    if have("canada_grants.csv"):
        w("<h3>Government of Canada contributions</h3>")
        table(w, ["Reference", "Recipient", "Organization", "Value (Canadian dollars)", "Date"], [list(x.values()) for x in rows(D, "canada_grants.csv")], num_from=9)
    if have("open_payments.csv"):
        w("<h3>CMS Open Payments</h3>")
        table(w, ["Program year", "Kind", "Payments", "Amount"], [[x["program_year"], x["kind"], x["payments"], f"${float(x['amount_usd']):,.2f}"] for x in rows(D, "open_payments.csv")], num_from=2)
        table(w, ["Nature of payment", "Payments", "Amount"], [[x["nature"], x["payments"], f"${float(x['amount_usd']):,.2f}"] for x in rows(D, "open_payments_nature.csv")])
        table(w, ["State", "Payments"], [[x["state"], x["payments"]] for x in rows(D, "open_payments_state.csv")])
    if have("uspto_assignments.csv"):
        w("<h3>USPTO assignments</h3>")
        table(w, ["Execution date", "Conveyance", "Assignors", "Assignees"], [[x["execution_date"], x["conveyance"], x["assignors"][:50], x["assignees"][:50]] for x in rows(D, "uspto_assignments.csv")][:25], num_from=9)
    if have("agency_statements.csv"):
        w("<h3>Federal Register</h3>")
        table(w, ["Document", "Sentence"], [[x["document"], x["sentence"]] for x in rows(D, "agency_statements.csv")][:12], num_from=9)
