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
    matchers/       exact and fuzzy matcher classes
    variants.py     automatic alias / variant generation and false-positive filtering
    normalise.py    text normalisation with offsets mapped back to the original
    dedupe.py       collapses same-spelling company rows, drops excluded names before matching
    disambiguate.py common-word disambiguation (false-positive removal) with batched LLM calls
    llm.py          OpenRouter resolution of document-local company references
    context.py      sentence / paragraph boundaries and context snippets
    definitions.py  document-local definitions -> scoped aliases (no LLM)
    mention.py      result types shared by matchers, runner and output
    output.py       summaries and writers (JSON, CSV, console table)
    sec.py          SEC/EDGAR helpers: filing sections, SGML submissions, Form D fields
    config.py       defaults (stop words, legal suffixes); data/common_words.txt is its word list

NOTES
-----
- Paths inside the scripts are relative to the folder layout above. Keep it as is.
- Every LLM-extracted value keeps a verbatim quote checked against the filing text. Spot-check facts.csv before
  sharing the report.
