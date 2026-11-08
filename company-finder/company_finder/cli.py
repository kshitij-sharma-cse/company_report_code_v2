"""Command line interface."""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config
from .extract import ExtractOptions, tesseract_available
from .dedupe import collect_excludes
from .disambiguate import disambiguate_common_words
from .llm import load_env_file
from .match import CompanyMatcher
from .mention import dedupe_output, dedupe_passages
from .output import build_report, format_table, write_csv, write_json
from .runner import discover, load_prior_hit_pages, run, run_llm_pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="company_finder",
        description="Find mentions of companies from a master list inside documents "
                    "(PDF, DOCX, XLSX, CSV, PPTX, HTML/iXBRL, TXT/MD, EDGAR .txt submissions, "
                    "Form D XML).")
    p.add_argument("--companies", "-c", required=True, nargs="+",
                   help="company master file(s) (.csv, .xlsx/.xlsm, .json, or "
                        "one-name-per-line .txt); several files are combined "
                        "(ids must be unique). Excel loads every sheet that has "
                        "a canonical_name/name header unless --companies-sheet is set")
    p.add_argument("--companies-sheet", action="append", default=None, metavar="NAME",
                   help="Excel worksheet to load (repeat the flag or use commas: "
                        "Public,Private). Missing names are an error. Ignored for "
                        "non-Excel company files. Default: all usable sheets")
    p.add_argument("--dedupe-companies", action="store_true",
                   help="collapse same-spelling Master rows before matching "
                        "(body + suffix class: Inc/INC. merge; Inc stays separate "
                        "from Limited/Corp/LLC/(US)). This is list cleanup only — "
                        "bare PDF text is disambiguated by the matcher + "
                        "--dedupe-output (default ON), not by this flag")
    p.add_argument("--dedupe-output", action=argparse.BooleanOptionalAction, default=True,
                   help="DEFAULT ON. After mentions are built (and again after --llm), "
                        "collapse multiple company_ids on the same document+page+char "
                        "span to ONE row whose canonical legal form matches the matched "
                        "text (bare BlackRock -> BLACKROCK). Never groups by matched_text "
                        "alone; spanless LLM rows stay unmerged. Different pages stay "
                        "separate. Does not merge Master Inc/Limited. "
                        "Use --no-dedupe-output to keep every ambiguous row")
    p.add_argument("--dedupe-passages", action=argparse.BooleanOptionalAction, default=True,
                   help="DEFAULT ON. Once a company's name or any alias matched a "
                        "paragraph, drop its other hits in that same paragraph "
                        "(same document+page): one row per company per paragraph. "
                        "Other surfaces go to extras.also_matched; mention counts "
                        "still include every hit. Different companies in one "
                        "paragraph stay separate. --no-dedupe-passages keeps every hit")
    p.add_argument("--mentions-detail", action=argparse.BooleanOptionalAction, default=True,
                   help="DEFAULT ON with --format csv. Also write <stem>_mentions.csv "
                        "beside -o with one row per hit (page, line, passage). "
                        "The -o file itself is the readable merge: one row per "
                        "company per document. --no-mentions-detail skips the detail file")
    p.add_argument("--exclude", action="append", default=None, metavar="NAME",
                   help="drop companies whose canonical name or any alias "
                        "soft-matches NAME, before matching (repeatable). "
                        "Example: --exclude BlackRock drops Inc, Limited, and (US)")
    p.add_argument("--exclude-file", metavar="PATH",
                   help="extra exclude names, one per line (# comments allowed)")
    p.add_argument("--input", "-i", nargs="+", help="file(s) and/or folder(s) (folders are recursive)")
    p.add_argument("--out", "-o", help="output file (default: stdout)")
    p.add_argument("--format", "-f", choices=["json", "csv", "table"], default="json")
    p.add_argument("--config", help="JSON file overriding matching settings (stop words, suffixes, ...)")
    p.add_argument("--fuzzy", action="store_true", help="enable fuzzy matching of capitalised n-grams")
    p.add_argument("--fuzzy-threshold", type=float, default=None, help="fuzzy score cutoff 0-100 (default 90)")
    p.add_argument("--llm", action="store_true",
                   help="resolve leftover company references via OpenRouter "
                        "(needs OPENROUTER_API_KEY; default scope is NOT the whole document)")
    p.add_argument("--llm-from", metavar="PATH",
                   help="Phase-1 JSON/CSV from a previous run; LLM uses those hit pages "
                        "(implies --llm). Re-extracts locally, does not resend the full filing")
    p.add_argument("--llm-scope", choices=["hits_and_tail", "full"], default=None,
                   help="hits_and_tail (default): Phase-1 hit pages + short section tail; "
                        "full: whole document (explicit, can be hundreds of calls)")
    p.add_argument("--llm-tail-pages", type=int, default=None,
                   help="pages after a hit to include in the default LLM scope (default 2)")
    p.add_argument("--llm-model", default=None,
                   help="OpenRouter model slug (default openai/gpt-4.1)")
    p.add_argument("--llm-chunk-chars", type=int, default=None,
                   help="maximum extracted text per OpenRouter request (default 10000)")
    p.add_argument("--llm-disambiguate", action="store_true",
                   help="batched OpenRouter calls to resolve every matched mention (uses the "
                        "company's 'description' column when present, name + excerpt alone "
                        "otherwise); drops a mention if the model confirms it is not the "
                        "company (needs OPENROUTER_API_KEY)")
    p.add_argument("--llm-disambiguate-batch-size", type=int, default=None,
                   help="candidate mentions packed per disambiguation call (default 6)")
    p.add_argument("--llm-disambiguate-workers", type=int, default=None,
                   help="concurrent disambiguation calls (default 6)")
    p.add_argument("--llm-disambiguate-max-rpm", type=int, default=None,
                   help="max OpenRouter requests per minute, shared by all workers "
                        "(default 18; 0 = no pacing). New OpenRouter accounts are capped at 20")
    p.add_argument("--llm-disambiguate-min-confidence", type=int, default=None,
                   help="a 'yes' below this confidence (0-100) is treated as 'no' (default 50)")
    p.add_argument("--min-score", type=float, default=0.0,
                   help="drop mentions scoring below this (ambiguous words at sentence start score 60)")
    p.add_argument("--ocr", action="store_true", help="OCR PDF pages with little/no text (needs tesseract)")
    p.add_argument("--ocr-lang", default="eng")
    p.add_argument("--no-strip-headers", action="store_true",
                   help="keep repeated PDF headers/footers")
    p.add_argument("--no-dehyphenate", action="store_true")
    p.add_argument("--workers", "-w", type=int, default=None,
                   help="parallel processes across files (default: min(files, CPUs, 8))")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--no-cache", action="store_true", help="do not read/write the automaton cache")
    p.add_argument("--cache-dir", help="cache folder (default ~/.cache/company_finder)")
    p.add_argument("--context-chars", type=int, default=None, help="max snippet length (default 300)")
    p.add_argument("--dump-text", metavar="DIR", help="write extracted text per document to DIR")
    p.add_argument("--show-aliases", action="store_true",
                   help="print generated aliases (and rejected ones) and exit")
    p.add_argument("--max-console-mentions", type=int, default=40)
    p.add_argument("--quiet", "-q", action="store_true", help="no summary on stderr")
    p.add_argument("--version", action="version", version=f"company_finder {__version__}")
    return p


def _flatten_mentions(results, *, do_dedupe: bool, do_passages: bool = True):
    mentions = [m for r in results for m in r.mentions]
    if not do_dedupe and not do_passages:
        return mentions
    kept = dedupe_output(mentions) if do_dedupe else mentions
    if do_passages:
        kept = dedupe_passages(kept)
    kept_ids = {id(m) for m in kept}
    for r in results:
        r.mentions = [m for m in r.mentions if id(m) in kept_ids]
    return kept


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cfg = Config.from_file(args.config)
    if args.fuzzy:
        cfg.fuzzy = True
    if args.fuzzy_threshold is not None:
        cfg.fuzzy_threshold = args.fuzzy_threshold
    prior_pages = None
    prior_inputs: list[str] = []
    if args.llm_from:
        prior_pages, prior_inputs = load_prior_hit_pages(args.llm_from)
        cfg.llm_references = True
    if args.llm:
        cfg.llm_references = True
    if args.llm_scope is not None:
        cfg.llm_scope = args.llm_scope
    if args.llm_tail_pages is not None:
        cfg.llm_tail_pages = args.llm_tail_pages
    if args.llm_model is not None:
        cfg.llm_model = args.llm_model
    if args.llm_chunk_chars is not None:
        cfg.llm_chunk_chars = args.llm_chunk_chars
    if args.llm_disambiguate:
        cfg.llm_disambiguate_common_words = True
    if args.llm_disambiguate_batch_size is not None:
        cfg.llm_disambiguate_batch_size = args.llm_disambiguate_batch_size
    if args.llm_disambiguate_workers is not None:
        cfg.llm_disambiguate_workers = max(1, args.llm_disambiguate_workers)
    if args.llm_disambiguate_max_rpm is not None:
        cfg.llm_disambiguate_max_rpm = max(0, args.llm_disambiguate_max_rpm)
    if args.llm_disambiguate_min_confidence is not None:
        cfg.llm_disambiguate_min_confidence = args.llm_disambiguate_min_confidence
    if args.context_chars is not None:
        cfg.context_max_chars = args.context_chars

    t0 = time.perf_counter()
    exclude_names = collect_excludes(args.exclude, args.exclude_file)
    matcher = CompanyMatcher.from_file(args.companies, cfg, cache_dir=args.cache_dir,
                                       use_cache=not args.no_cache,
                                       sheets=args.companies_sheet,
                                       dedupe=args.dedupe_companies,
                                       exclude=exclude_names)
    t_load = time.perf_counter() - t0

    if args.show_aliases:
        rows = matcher.alias_table()
        print(f"{'company_id':<14} {'case':<12} {'source':<9} variant")
        exch: dict[str, set] = {}
        for r in rows:
            if r["case_mode"] == "ticker":  # one line per company instead of one per exchange
                exch.setdefault(r["company_id"], set()).add(r["origin"])
                continue
            print(f"{r['company_id']:<14} {r['case_mode']:<12} {r['source']:<9} {r['variant']}")
        if exch:
            print("\nTickers matched after an exchange prefix (e.g. 'NYSE: X'):")
            for cid, tks in exch.items():
                print(f"{cid:<14} {', '.join(sorted(tks))}")
        rej = matcher.rejected_table()
        if rej:
            print("\nRejected variants:")
            for r in rej:
                print(f"{r['company_id']:<14} {r['variant']!r:<30} ({r['reason']}; from {r['origin']!r})")
        if matcher.block_keys:
            print("\nBlock phrases:", ", ".join(matcher.block_keys))
        return 0

    if (cfg.llm_references or cfg.llm_disambiguate_common_words) and \
            not os.environ.get("OPENROUTER_API_KEY"):
        print("error: --llm / --llm-disambiguate requires OPENROUTER_API_KEY "
              "(set env or copy .env.example to .env)", file=sys.stderr)
        return 2

    if not args.input:
        if prior_inputs:
            args.input = prior_inputs
        else:
            print("error: --input is required (unless --show-aliases)", file=sys.stderr)
            return 2
    files = discover(args.input, recursive=not args.no_recursive)
    if not files:
        print("error: no supported documents found", file=sys.stderr)
        return 1
    opts = ExtractOptions(ocr=args.ocr, ocr_lang=args.ocr_lang,
                          strip_headers=not args.no_strip_headers,
                          dehyphenate=not args.no_dehyphenate)
    if args.ocr:
        ok, msg = tesseract_available()
        if not ok:
            print(f"warning: --ocr requested but {msg}; scanned pages will be skipped",
                  file=sys.stderr)

    t1 = time.perf_counter()
    results = run(matcher, files, opts, workers=args.workers, min_score=args.min_score,
                  dump_text_dir=args.dump_text)
    t_match = time.perf_counter() - t1

    def _documents():
        return [{"path": r.path, "doc_type": r.doc_type, "units": r.units,
                 "text_chars": r.text_chars, "mentions": len(r.mentions),
                 "seconds": round(r.seconds, 3), "info": r.info, "warnings": r.warnings,
                 "error": r.error, "filer": r.filer,
                 "filer_company_ids": r.filer_company_ids} for r in results]

    def _meta(llm_on: bool, extra_timing: dict | None = None):
        timing = {"load_matcher": round(t_load, 4), "extract_and_match": round(t_match, 4)}
        if extra_timing:
            timing.update(extra_timing)
        return {
            "tool": "company_finder", "version": __version__,
            "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "companies_file": [str(c) for c in args.companies], "companies": len(matcher.companies),
            "dedupe_companies": bool(args.dedupe_companies),
            "dedupe_output": bool(args.dedupe_output),
            "dedupe_passages": bool(args.dedupe_passages),
            "exclude": exclude_names,
            "aliases": len(matcher.specs), "inputs": [str(x) for x in args.input],
            "files": len(files), "fuzzy": cfg.fuzzy, "fuzzy_threshold": cfg.fuzzy_threshold,
            "llm_references": llm_on,
            "llm_model": cfg.llm_model if llm_on else None,
            "llm_scope": cfg.llm_scope if llm_on else None,
            "llm_from": args.llm_from if llm_on else None,
            "llm_disambiguate_common_words": cfg.llm_disambiguate_common_words,
            "definition_aliases": cfg.definition_aliases,
            "ocr": args.ocr, "min_score": args.min_score,
            "cache_hit": matcher.cache_info.get("cache_hit", False),
            "timing_seconds": timing,
        }

    def _emit(report):
        if args.format == "json":
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    write_json(report, fh)
            else:
                write_json(report, sys.stdout)
        elif args.format == "csv":
            written = write_csv(report, args.out, mentions_detail=args.mentions_detail)
            if written and not args.quiet:
                print("wrote " + ", ".join(written), file=sys.stderr)
        else:
            table = format_table(report, args.max_console_mentions)
            if args.out:
                Path(args.out).write_text(table + "\n", encoding="utf-8")
            else:
                print(table)

    # Phase-1 file hits disk before any OpenRouter call so Ctrl+C during LLM
    # still leaves the deterministic mentions (when --out is a file).
    mentions = _flatten_mentions(results, do_dedupe=args.dedupe_output,
                                 do_passages=args.dedupe_passages)
    report = build_report(mentions, _documents(),
                          _meta(False))
    if cfg.llm_references and args.out:
        _emit(report)
        if not args.quiet:
            print(f"phase-1 written to {args.out} "
                  f"(LLM scope={cfg.llm_scope}; full-doc only with --llm-scope full)",
                  file=sys.stderr)
    if cfg.llm_references:
        t2 = time.perf_counter()
        run_llm_pass(matcher, results, opts, scope=cfg.llm_scope,
                     tail_pages=cfg.llm_tail_pages, prior_pages=prior_pages,
                     min_score=args.min_score)
        t_llm = time.perf_counter() - t2
        mentions = _flatten_mentions(results, do_dedupe=args.dedupe_output,
                                 do_passages=args.dedupe_passages)
        report = build_report(mentions, _documents(),
                              _meta(True, {"llm": round(t_llm, 4)}))

    if cfg.llm_disambiguate_common_words:
        t3 = time.perf_counter()
        disambig_warnings: list[str] = []
        disambig_stats: dict = {}
        mentions = disambiguate_common_words(
            matcher, mentions, model=cfg.llm_model,
            timeout=cfg.llm_disambiguate_timeout_seconds,
            batch_size=cfg.llm_disambiguate_batch_size,
            min_confidence=cfg.llm_disambiguate_min_confidence, warnings=disambig_warnings,
            workers=cfg.llm_disambiguate_workers, retries=cfg.llm_disambiguate_retries,
            stats=disambig_stats, max_rpm=cfg.llm_disambiguate_max_rpm)
        t_disambig = time.perf_counter() - t3
        kept_ids = {id(m) for m in mentions}
        for r in results:
            r.mentions = [m for m in r.mentions if id(m) in kept_ids]
        report = build_report(mentions, _documents(),
                              _meta(cfg.llm_references, {"llm_disambiguate": round(t_disambig, 4)}))
        if disambig_warnings and not args.quiet:
            for w in disambig_warnings:
                print(f"warning: {w}", file=sys.stderr)
        # Printed even with --quiet: unverified mentions are kept in the output, so
        # the user must always be told when the LLM pass did not cover everything.
        if disambig_stats.get("unverified"):
            print(f"WARNING: llm_disambiguate: {disambig_stats['unverified']} of "
                  f"{disambig_stats['candidates']} mention(s) could NOT be verified by the "
                  f"LLM and were kept unchecked (extras.llm_disambiguation.status="
                  f"'unverified'). Rerun, or lower --llm-disambiguate-workers.",
                  file=sys.stderr)
    _emit(report)

    if not args.quiet and (args.out or args.format != "table"):
        print(format_table(report, max_mentions=0), file=sys.stderr)
        extra = ""
        if cfg.llm_references:
            extra = f", llm {report['meta']['timing_seconds'].get('llm', 0):.3f}s scope={cfg.llm_scope}"
        print(f"[{len(files)} file(s), load {t_load:.3f}s "
              f"({'cache' if report['meta']['cache_hit'] else 'built'}), "
              f"extract+match {t_match:.3f}s{extra}]",
              file=sys.stderr)
    return 1 if all(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
