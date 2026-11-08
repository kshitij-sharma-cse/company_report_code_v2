#!/usr/bin/env python3
"""CLI entry point:  python company_finder.py --companies companies.csv --input docs/ --out results.json"""
import sys

from company_finder.cli import main

if __name__ == "__main__":
    sys.exit(main())
