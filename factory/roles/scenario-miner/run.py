#!/usr/bin/env python3
"""Thin wrapper for the Scenario Miner role (spec §5). Reads production session
logs, writes CANDIDATE scenarios to staging for operator vetting."""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
from factory.common.store import Blackboard
from factory.roles.common import mine_scenarios

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    with Blackboard() as store:
        for p in mine_scenarios(store, a.limit):
            print(p)
