COMPANY REPORT CODE
===================

Builds a "financials year by year" report for a private company (modelled on the Databricks report).
Sources: the SEC Form D database, plus the SEC filings of other companies and funds that mention the company.
Values the sources cannot give appear as placeholders (R1, R2, ...). The report ends with a table listing the
form or record needed to fill each placeholder.

This zip holds code only. It contains no data, filings, outputs, API keys or .md files.


FOLDER LAYOUT (copy the contents of company_report_code/ into the project root, "Project x latest/")
-----------------------------------------------------------------
company-finder/                     upstream pipeline that finds company mentions in filings
  company_finder.py                 its command-line entry point
  company_finder/                   the package (the HTML/PDF reader is also used by the steps below)
  requirements.txt
coref_experiment/
  common.py, approach_d_relations.py      shared library
  steps/shared.py, steps/relationships.py relationship step
  coref_verifier/tools/prepare_targeted.py prepares one company's filings
  company_report/build_report.py           THE REPORT SCRIPT (runs everything below in one go)


SETUP
-----
1. Python 3.11+, in a virtual environment:
     cd "Project x latest/company-finder"
     python3 -m venv .venv
     .venv/bin/pip install -r requirements.txt requests duckdb
2. API key: create company-finder/.env with this line (it is not in the zip):
     OPENROUTER_API_KEY=sk-or-...
3. Form D database (not in the zip):
     Project x latest/Project X Resources/formd_db_2026-10-20/formd.duckdb
   To use another location, pass --formd-db PATH.
4. Google Chrome installed at /Applications/Google Chrome.app, used to print the PDF. Without it you get HTML only.
5. Filings and the upstream mention lists (not in the zip):
     Resources/Targeted_pdf/dumps/<company>/<form>/<file>   + dumps/<company>_manifest.csv
     Resources/Targeted_pdf/<run>/out/<company>_*/<company>_mentions.csv


USAGE
-----
Run from the project root. Keep the quotes: the folder name has spaces.

  cd "/Users/mohit/Downloads/company-finder/Project x latest"
  company-finder/.venv/bin/python coref_experiment/company_report/build_report.py kalshi --cik 0001806928

Options:
  --name "Kalshi Inc."        display / matching name (default: the name in Form D)
  --formd-db PATH             another Form D database file
  --model openai/gpt-4.1      model for the LLM steps (through OpenRouter)
  --refresh-facts             re-run the fact extraction (otherwise cached per filing)
  --rerun-relationships       re-run the relationship step (otherwise cached per filing)
  --no-llm                    no model calls: Form D + whatever is cached
  --no-pdf                    HTML only

Cost and time for Kalshi (27 filings): about $0.25 and 3-4 minutes on the first run; reruns from cache take about
2 seconds and cost $0.

Typical order for a NEW company:
  0. company-finder/.venv/bin/python tools/make_companies_file.py <company>      (-> companies file with description)
  1. company-finder/.venv/bin/python company-finder/company_finder.py --companies companies.csv \
         --input Resources/Targeted_pdf/dumps/<company> --llm --out <run>/out/<company>_x/<company>_mentions.csv --format csv
  2. company-finder/.venv/bin/python coref_experiment/coref_verifier/tools/prepare_targeted.py <company>
  3. company-finder/.venv/bin/python coref_experiment/company_report/build_report.py <company> --cik <CIK>
  (build_report.py runs step 2 itself if the company has no prepared filings.)


SCRIPTS: what each does, its input and its output
-------------------------------------------------
coref_experiment/company_report/build_report.py
  What:   one command for the whole report. Runs Form D SQL -> relationships -> LLM fact and holdings extraction
          (tables included; N-CEN parsed from its XML) -> merge -> HTML/PDF with placeholders and the
          required-forms table.
  Input:  company folder name, the company's CIK, formd.duckdb, the prepared filings in
          coref_verifier/data/{pdfs,companies}, OPENROUTER_API_KEY
  Output: coref_experiment/company_report/out/<company>/
            <company>_financials_year_by_year.html / .pdf   the report
            formd_offerings.csv   one row per Form D offering
            holdings.csv          fund positions: shares, cost, fair value, price per share, as-of date, source
            facts.csv             dated facts (legal events, regulatory status, transactions ...) with quotes
            relationships_all.csv every relationship row from all filings, with direction_check
            requirements.csv      placeholder id -> missing value, required form, where to get it, what it unlocks
            facts/<filing>.json, relationships/<filing>_*.csv   per-filing cache

coref_experiment/steps/relationships.py
  What:   finds business relationships between the listed companies and the filer (invests in, supplies, acquired,
          competes with ...), with a verbatim quote and a direction check (the model names the company that acts;
          the code flips any edge that contradicts it).
  Input:  a filing (HTML/PDF/XML), its *_mentions.csv, --filer, --form, --filing-date, --out-dir
  Output: <filing>_relationships.csv, <filing>_relationship_items.csv (every decision),
          <filing>_relationships_review.csv (unclear links), <filing>_relationships_run.json (cost, counts)

coref_experiment/steps/shared.py
  What:   shared step code: loads the filing and its mentions ("scope"), filing context for prompts, trigger words.
  Input:  filing path + mentions file (called by other scripts)
  Output: none (library)

coref_experiment/common.py
  What:   filing readers (PDF via PyMuPDF; HTML/XML via company_finder.extract), paragraph splitting, OpenRouter LLM
          calls with retries, rate limiter, cost totals.
  Input:  filing paths, company-finder/.env (API key)
  Output: none (library)

coref_experiment/approach_d_relations.py
  What:   loads pipeline mentions, locates them in paragraphs, picks the filer, detects table paragraphs, text
          normalisation.
  Input:  *_mentions.csv / final.json (called by other scripts)
  Output: none (library)

coref_experiment/coref_verifier/tools/prepare_targeted.py
  What:   matches a company's downloaded filings with the upstream mentions and writes one mentions file + metadata
          per filing.
  Input:  Resources/Targeted_pdf/dumps/<company>_manifest.csv, dumps/<company>/..., <company>_mentions.csv
  Output: coref_verifier/data/pdfs/<filing> (copy),
          coref_verifier/data/companies/<company>__<form>__<accession>_mentions.csv and .meta.json
          (form, date, filer, SEC url)

company-finder/company_finder.py  (+ package company_finder/)
  What:   upstream pipeline: exact (optionally fuzzy) match of company names and aliases, paragraph de-duplication,
          LLM removal of false positives.
  Input:  --companies <csv/xlsx of companies>, --input <files or folders>, optional --llm
  Output: --out <json/csv>: one row per kept mention (company, matched text, page, paragraph, context)
  Modules, one line each:
    cli.py          command-line options
    runner.py       runs the matcher over files / folders, optionally in parallel
    loader.py       loads the company list (CSV, JSON, Excel, TXT)
    extract.py      text from PDF / HTML / XML / DOCX / ...
    match.py        name matching (CompanyMatcher)
    matchers/       exact and fuzzy matcher classes. A match that is the tail of a longer capitalised name
                    ("A Work of Faith Community Outreach Corp" for "Outreach Corp": a Title-case word right before it,
                    one space, same line, not a function word or legal suffix) is dropped unless one of the
                    company's names contains that word pair (config drop_name_tail_matches, name_tail_skip_words).
                    When layout can explain the run-in it is only hinted (extras.name_tail, shown to
                    --llm-disambiguate): a gap wider than one space, PDF text (lines are joined), a heading run into
                    the name ("Application Software Databricks, Inc.": the line or cell up to the match is Title-case
                    words, commas and &/and only), or a bare name after one word ("Microsoft Databricks").
                    Dropped matches are written to <out>_dropped.csv (reason name_tail_rule, longer_name; the
                    --llm-disambiguate drops are there too, reason llm_disambiguate) and counted on stderr.
    variants.py     automatic alias / variant generation and false-positive filtering
    normalise.py    text normalisation with offsets mapped back to the original
    dedupe.py       collapses same-spelling company rows, drops excluded names before matching
    disambiguate.py common-word disambiguation (false-positive removal) with batched LLM calls; the prompt has
                    the company's known names and description
    llm.py          OpenRouter resolution of document-local company references
    context.py      sentence / paragraph boundaries and context snippets
    definitions.py  document-local definitions -> scoped aliases (no LLM)
    mention.py      result types shared by matchers, runner and output
    output.py       summaries and writers (JSON, CSV, console table)
    sec.py          SEC/EDGAR helpers: filing sections, SGML submissions, Form D fields
    config.py       defaults (stop words, legal suffixes); data/common_words.txt is its word list

PIPELINE TOOLS AND THE ONE-COMMAND RUN (tools/, run_company.py)
---------------------------------------------------------------
Every tool reads the company from coref_experiment/company_report/model_config/<co>.json ("name", company.cik,
company.aliases, company.name_regex; optional "dump_folders"). Nothing in them is specific to one company.
SEC asks every client to name itself; the SEC tools and nport.py stop with an error when this is not set:
  export SEC_USER_AGENT="your-org contact@your-domain"
All SEC access is at most 5 requests/s, with retries. <root> below is the folder that holds dumps/ and input/.

tools/download_filings.py <co> [--term "..."] [--skip-forms NPORT-P,NPORT-P/A] [--dumps DIR] [--list-only]
  Step 0. EDGAR full-text search (2001 on, all forms) for the config name + aliases (+ --term). Ranges over the
  search's 10,000-hit cap are split by date. Hits of --skip-forms are listed but not downloaded (default: N-PORT,
  which nport.py reads as XML; pass --skip-forms "" to keep all). Resumable: files on disk are kept.
  With --only-terms (search just the --term phrases) the new rows are merged into the existing manifest and hit list,
  so a run for one extra phrase keeps the filings found before. Both files are written atomically.
  Output: <dumps>/<co>/<form>/<accession>_<file>, <dumps>/<co>_manifest.csv
          (file,form,file_date,accession,filer,sec_url,index_url,status), <dumps>/<co>_fts_hits.csv (every hit, terms)

coref_experiment/company_report/nport.py
  list / download / parse   N-PORT XML from EDGAR (needs SEC_USER_AGENT; TLS via certifi)
  list [TERM] --dump DIR [--term T ...] [--config ...]
                            searches every name form: TERM, the config "name" and company.aliases, each --term (a
                            filing that prints only "Acme Corporation" is missed by a search for "Acme Corp"); hits are
                            merged by accession, and manifest column search_terms says which forms found each filing.
  export-db TERM --dump DIR --db nport_team.duckdb --config model_config/<co>.json [--match-title]
                            team database rows whose ISSUER_NAME matches (^|\W)(name_regex)(\W|$) -> DIR/duckdb_rows.csv
                            (whole words, so "get heal" does not match "TARGET HEALTHCARE"). Stops with exit 1 when
                            no row matches, and when name_regex is not RE2-compatible (DuckDB: no lookarounds).
  build TERM --dump DIR --config model_config/<co>.json --out DIR/nport_holdings.csv
                            database rows first, XML rows (DIR/nport_rows.csv) for filings the database lacks; either
                            file may be absent, not both. A row is the company's own equity when its issuer name starts
                            with name_regex; a vehicle named after it is spv_interest.
  Name pattern (parse, export-db, build): company.name_regex of --config; without --config, model_config/<co>.json
  with <co> from a DIR named <co>_nport or from the TERM's words; else the words of TERM and of the config "name" with
  any punctuation between them ("Get Heal, Inc." also matches "GET HEAL INC"), as alternatives. Aliases are never
  used here (a generic alias would let another issuer in as the company's own equity); list searches them, parse
  filters. The pattern used is printed. TERM is optional when the config is found.
  Usual order: export-db "<Name>" --dump <root>/dumps/<co>_nport --db ... --config model_config/<co>.json ;
               build "<Name>" --dump <root>/dumps/<co>_nport --config model_config/<co>.json
               --out <root>/dumps/<co>_nport/nport_holdings.csv

tools/make_companies_file.py <co> [--out FILE.csv] [--config FILE.json] [--name "Legal Name"] [--force]
  The company-finder input CSV (id, canonical_name, aliases, description) from the config: id C<cik>, canonical_name
  company.legal_name (else --name, else "name"), aliases "name" + company.aliases, company.description. Default --out:
  coref_experiment/Targeted_pdf/run1/input/<co>_input.csv. An existing file is never overwritten without --force: the
  company's row keeps its id, canonical_name and aliases (prepare_targeted.py copies canonical_name into every prepared
  filing) and only gets the description filled.
  company.description (what the company does, where, former names) goes into the --llm-disambiguate prompt, so a
  look-alike organisation ("... Community Outreach Corp", a hospital board) is rejected; without it the tool warns.

tools/build_formd_db.py <co> [--xml-dir DIR ...] [--out PATH] [--no-fetch] [--force]
  Only when the team's formd.duckdb is missing. Builds the same tables (companies, filings, related_persons,
  offerings) from Form D XML: the company's own D and D/A notices (listed from its EDGAR filing index, missing ones
  downloaded into the first --xml-dir), plus any other issuer's notice in --xml-dir (an SPV naming the company;
  category 'fund' when it says Pooled Investment Fund). Filing date and file number come from the EDGAR index. An
  amendment restates its offering, so offering and company totals use the latest notice per file number.
  Default --xml-dir: <root>/dumps/<co>/D and D-A; default --out: <root>/input/formd/formd.duckdb (never overwritten
  without --force). Stops before writing when no parsed notice is the company's own (CIK from the config).

tools/build_context_windows.py <co> [--folders ...] [--dumps DIR] [--context 50] [--out FILE.xlsx]
  Workbook for evidence_recall.py: sheet "Windows" (Window ID, Form, Filing date, Company (filer), Accession number,
  File name, Mention lines, Line range, LLM input (metadata + window text)). Up to --context lines (paragraphs, table
  rows, PDF lines) either side of each match of company.name_regex; mention lines start with >>> and the name is in
  « ». HTML/XML whitespace is collapsed before lines are cut at block tags, so a name wrapped in the source
  ("Get\n Heal Inc.") is one line; in plain text and PDF a name cut over two lines joins them. A window longer than an
  Excel cell is cut with less context; a single line longer than a cell is cut around its first mention.

coref_experiment/company_report/run_company.py <co> --cik CIK --name "..." [options]
  1. input tables (skip with --skip-inputs)   2. evidence by rule: evidence_tables.py --folders <dump folders>
  --workers N, then evidence_build.py (skip with --no-evidence)   3. build_report.py --evidence-rows
  out/<co>/evidence/evidence_rows.csv   4. with --context-windows FILE: evidence_recall.py and evidence_workbook.py
  (workbooks in out/<co>/evidence/).
  Dump folders: --folders, else config "dump_folders", else <co> plus every <co>_<word> with a manifest
  (e.g. <co>_ncsr, <co>_delta). An extra folder goes to build_report.py only if it has a company-finder mentions run
  (prepared filings, or a Targeted_pdf mentions csv plus Targeted_pdf/dumps/<f>_manifest.csv); otherwise it is used by
  the evidence steps only and listed under "not built". A requested folder without a manifest is listed there too.
  A config key a step needs but the company lacks (peers, k401_plan ...) is listed as "not configured" at the end.
  WARNING (at the start and again at the end) when the config has no company.description, or when the
  company-finder input (--companies-file, else Targeted_pdf/*/input/<co>_input.csv, the file prepare_targeted.py
  reads) has no description column or an empty description. A file it cannot read is a warning, never a stop.

New company, in order:
  tools/download_filings.py <co>                     -> dumps/<co>, dumps/<co>_manifest.csv
  nport.py export-db + build                          -> dumps/<co>_nport/nport_holdings.csv
  tools/make_companies_file.py <co>                  -> Targeted_pdf/run1/input/<co>_input.csv
  company_finder.py ... ; prepare_targeted.py <co>    (see "Typical order" above)
  tools/build_formd_db.py <co>                        only without the team's formd.duckdb
  tools/build_context_windows.py <co> --out <file>
  run_company.py <co> --cik ... --name ... --nport-db ... --context-windows <file>


NOTES
-----
- Paths inside the scripts are relative to the folder layout above. Keep it as is.
- Every LLM-extracted value keeps a verbatim quote checked against the filing text. Spot-check facts.csv before
  sharing the report.
