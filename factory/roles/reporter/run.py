#!/usr/bin/env python3
"""Thin wrapper for the Reporter role (spec §5). Prepares a promotion digest +
deterministic divergence signals for one candidate."""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
from factory.common.store import Blackboard
from factory.roles.common import report

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate_id")
    a = ap.parse_args()
    with Blackboard() as store:
        print(report(store, a.candidate_id))
