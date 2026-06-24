"""The operator's board (spec §10): a tiny local server over the blackboard.

Read-mostly. Exactly ONE write action: promotion. Bound to 127.0.0.1. This is the
andon board where the operator stands outside the loop — not a progress animation.

Run:  python3 -m factory.dashboard.server   (then open http://127.0.0.1:8787)
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..common import config, paths, scoring
from ..common.store import Blackboard

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CHAMPION_ID = "champion"


def build_state() -> dict:
    with Blackboard() as store:
        store.init_db()   # idempotent (IF NOT EXISTS) — a pre-init board shows empty, not 500
        champ = store.get_champion()
        champ_id = champ["id"] if champ else None
        cands = store.list_candidates()

        # Kanban
        stages = ("proposed", "evaluating", "scored", "awaiting_gate", "promoted", "rejected")
        kanban = {s: [] for s in stages}
        for c in cands:
            scores = json.loads(c["scores_json"] or "{}")
            kanban.setdefault(c["stage"], []).append({
                "id": c["id"], "parent": c["parent"],
                "change": c["change_summary"], "scores": scores})

        # Scoreboard (champion + scored/queued challengers x panel)
        panel = [m["name"] for m in config.panel_models()]
        def row(cid, label):
            sc = scoring.candidate_scores(store, cid)
            return {"id": cid, "label": label, "working": sc["working_set"],
                    "held_out": sc["held_out"], "panel": sc["panel_rates"],
                    "spread": sc["panel_spread"], "safety": sc["safety_tripped"]}
        scoreboard = {"panel": panel, "rows": [row(CHAMPION_ID, "champion (baseline)")]}
        for c in cands:
            if c["id"] != CHAMPION_ID and c["stage"] in ("scored", "awaiting_gate", "promoted"):
                scoreboard["rows"].append(row(c["id"], c["change_summary"][:40] or c["id"]))

        # Divergence alarms
        divergence = []
        for c in cands:
            if c["id"] != CHAMPION_ID and c["stage"] in ("scored", "awaiting_gate"):
                d = scoring.divergence_signal(store, c["id"], champ_id)
                divergence.append({"id": c["id"], **d})

        # Held-out leakage meter
        thresh = config.load_config().get("held_out", {}).get("leakage_threshold", 5)
        leakage = [{"id": s["id"], "leakage": s["leakage_count"], "threshold": thresh,
                    "active": bool(s["active"])}
                   for s in store.list_scenarios(partition="held-out", active_only=False)]

        # Cost burn
        bt = store.budget_totals()
        round_cap = config.load_config().get("budget", {}).get("round_max_tokens", 400000)
        cost = {"tokens": bt["tokens"], "cost": bt["cost"], "round_cap": round_cap,
                "ledger": store.budget_entries()[-30:]}

        # Promotion queue (cleared §9 -> awaiting human gate)
        queue = []
        for c in store.list_candidates("awaiting_gate"):
            promo = scoring.evaluate_promotion(store, c["id"], champ_id,
                                               config.load_config())
            digest_path = os.path.join(paths.RUNS_DIR, f"{c['id']}.digest.md")
            digest = ""
            if os.path.exists(digest_path):
                with open(digest_path, "r", encoding="utf-8", errors="replace") as fh:
                    digest = fh.read()
            queue.append({"id": c["id"], "change": c["change_summary"],
                          "promotion": promo, "digest": digest})

        # Safety flags
        safety = store.all_safety_flags()

        return {"champion": champ, "kanban": kanban, "scoreboard": scoreboard,
                "divergence": divergence, "leakage": leakage, "cost": cost,
                "queue": queue, "safety": safety,
                "phase": "Phase 0 — promotion is a human action; nothing promotes "
                         "automatically; no real credentials"}


def do_promote(payload: dict) -> tuple[int, dict]:
    """The ONE write action. Promote a candidate that cleared the gate."""
    cid = payload.get("candidate_id")
    operator = (payload.get("operator") or "").strip()
    rationale = (payload.get("rationale") or "").strip()
    if not cid or not operator:
        return 400, {"error": "candidate_id and operator are required"}
    with Blackboard() as store:
        store.init_db()
        cand = store.get_candidate(cid)
        if not cand:
            return 404, {"error": f"no such candidate {cid}"}
        if cand["stage"] != "awaiting_gate":
            return 409, {"error": f"candidate {cid} is '{cand['stage']}', not "
                         f"'awaiting_gate'; only queued candidates can be promoted"}
        scores = scoring.candidate_scores(store, cid)
        store.add_promotion(cid, "promote", operator, rationale)
        store.set_stage(cid, "promoted")
        # Keep the champion id stable as CHAMPION_ID but repoint its spec at the
        # promoted candidate, so the loop (which evaluates CHAMPION_ID) runs the
        # promoted spec. Runtime promotion: `reset` reverts to the seed champion.yaml;
        # to make it durable, fold the change into champion.yaml + commit.
        store.set_champion(CHAMPION_ID, cand["spec_path"], scores=scores)
        return 200, {"ok": True, "champion": f"{CHAMPION_ID} -> {cid}", "operator": operator}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"),
                   "application/json")

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._static("app.js", "application/javascript")
        if path == "/style.css":
            return self._static("style.css", "text/css")
        if path == "/api/state":
            try:
                return self._json(200, build_state())
            except Exception as e:
                return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def _local_origin(self) -> bool:
        """Reject cross-origin writes (CSRF defense). The board binds localhost but
        is reachable from a browser, so a malicious site could POST here. Allow
        only same-origin (no Origin header) or an explicit localhost Origin."""
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True
        try:
            host = urlparse(origin).hostname  # strips scheme/port/path; unwraps [::1]
        except Exception:
            return False
        # Exact host match — substring matching would let localhost.attacker.com pass.
        return host in ("127.0.0.1", "localhost", "::1")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/promote":
            return self._json(404, {"error": "the only write action is /api/promote"})
        if not self._local_origin():
            return self._json(403, {"error": "cross-origin promotion refused (CSRF guard)"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        code, body = do_promote(payload)
        return self._json(code, body)

    def _static(self, name: str, ctype: str) -> None:
        p = os.path.join(STATIC, name)
        if not os.path.exists(p):
            return self._json(404, {"error": f"{name} missing"})
        with open(p, "rb") as fh:
            self._send(200, fh.read(), ctype)


def main() -> int:
    cfg = config.load_config().get("dashboard", {})
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8787))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"clive-harness-factory board on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nboard stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
