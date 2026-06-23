#!/usr/bin/env python3
"""Thin wrapper for the Proposer role (spec §5). Reads champion + failures +
tried changes from the store, calls `claude -p`, writes one candidate."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
from factory.common.store import Blackboard
from factory.roles.common import propose

if __name__ == "__main__":
    with Blackboard() as store:
        cid = propose(store)
        print(cid or "(no candidate produced)")
