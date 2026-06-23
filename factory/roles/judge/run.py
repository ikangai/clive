#!/usr/bin/env python3
"""Thin wrapper for the Judge role (spec §5). Annotates one run AFTER grading."""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
from factory.common.store import Blackboard
from factory.roles.common import judge

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    a = ap.parse_args()
    with Blackboard() as store:
        print(judge(store, a.run_id))
