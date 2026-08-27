#!/usr/bin/env python3
"""Export metrics from SQLite for the last N days to CSV.
Usage: python scripts/export_metrics.py --days 90 --out /tmp/metrics.csv
"""
import argparse
from src.observability import AgentObserver

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=90)
parser.add_argument("--out", default=None)
args = parser.parse_args()

obs = AgentObserver()
path = obs.export_csv(days=args.days, out_path=args.out)
print("Exported to:", path)
