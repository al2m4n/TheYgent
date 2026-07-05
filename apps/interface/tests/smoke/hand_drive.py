"""Repeatable hand-drive smoke for apps/interface, driven against a LIVE stack.

The mocked Vitest tests prove the FE in isolation; they CANNOT prove the one thing that matters
most across the seam: that a pure layout change ("drag a node") leaves the **server-computed
contentHash** untouched, while a real content edit changes it — and that the edited agent still
RUNS via the walker (the path the durable runtime reuses). This script asserts exactly that against
the running control-plane, so it keeps paying off as the codebase evolves instead of being a
one-off click.

What it does (idempotent — uses a unique agent id per run):
  1. create an agent (input→output) at v0.1.0            → record contentHash h1
  2. RE-PUBLISH v0.1.0 with a DIFFERENT `view` (a drag)  → assert idempotent 200 AND hash == h1
     (the load-bearing proof: a view-only change is the SAME content. If `view` were hashed this
      would be a 409 version_conflict, not an idempotent re-publish. `version` IS hashed, so the
      drag must be tested under the same version — comparing two versions can't isolate `view`.)
  3. add v0.2.0 = a STRUCTURAL edit (relabel a node)     → assert hash != h1   (content hashes)
  4. GET v0.2.0 (reload)                                 → assert the IR round-trips
  5. run v0.2.0 via /agents/{id}/runs with an input      → assert it completes, output == input
     (the walker run path — execution unchanged by the canvas)

Prereqs: `make up` (control-plane on :8080 + its Postgres). No inference needed — an input→output
graph passes its input straight through, so this never depends on a model being resident.

Run:  make smoke-interface           (or:  uv run --package theygent-control-plane \
                                            python apps/interface/tests/smoke/hand_drive.py)
Env:  THEYGENT_CONTROL_PLANE_URL (default http://localhost:8080),
      THEYGENT_DEV_TOKEN (default dev-local)
Exit: 0 = all assertions held; non-zero = a regression (prints which assertion failed).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

BASE = os.environ.get("THEYGENT_CONTROL_PLANE_URL", "http://localhost:8080").rstrip("/")
TOKEN = os.environ.get("THEYGENT_DEV_TOKEN", "dev-local")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# A unique id per run so re-running never collides on create
# (agent identity is immutable after creation).
AGENT_ID = f"agent.smoke.{int(time.time())}"

_passed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        print(f"\nFAILED: {label}", file=sys.stderr)
        sys.exit(1)
    _passed += 1


def ir(version: str, *, view: dict[str, Any], in_label: str | None = None) -> dict[str, Any]:
    """An input→output agent (no inference needed). `view` carries layout; `in_label` is a content
    edit (a node label) used to force a different contentHash without needing a model/tool."""
    return {
        "schemaVersion": "1.0",
        "id": AGENT_ID,
        "name": "interface smoke",
        "version": version,
        "models": {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                **({"label": in_label} if in_label is not None else {}),
                "ports": {"in": [], "out": [{"id": "out"}]},
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            }
        ],
        "view": view,
    }


def latest_hash(detail: dict[str, Any]) -> str:
    return detail["versions"][0]["content_hash"]


def main() -> None:
    print(f"hand-drive smoke → {BASE} (agent {AGENT_ID})")
    with httpx.Client(base_url=BASE, headers=HEADERS, timeout=30.0) as c:
        # readiness — fail loud if the stack isn't up.
        try:
            c.get("/agents", params={"limit": 1}).raise_for_status()
        except Exception as exc:
            print(
                f"\ncontrol-plane not reachable at {BASE} — run `make up` first ({exc})",
                file=sys.stderr,
            )
            sys.exit(2)

        # 1. create
        view_a = {
            "nodes": {
                "n_in": {"position": {"x": 0, "y": 0}},
                "n_out": {"position": {"x": 240, "y": 0}},
            }
        }
        r = c.post("/agents", json={"ir": ir("0.1.0", view=view_a)})
        check("create v0.1.0 → 201", r.status_code == 201, f"status {r.status_code}")
        h1 = latest_hash(r.json())
        print(f"    contentHash h1 = {h1}")

        # 2. a pure DRAG: re-publish the SAME version with different positions. Because `view` is
        #    not hashed, this is the SAME content → an idempotent 200 (NOT a 409 version_conflict),
        #    and the hash is unchanged. This is the server-verified view-isolation invariant that
        #    a mocked test cannot give us. (Comparing two distinct versions can't isolate `view`
        #    — `version` is itself hashed, so a different version always yields a different hash.)
        view_b = {
            "nodes": {
                "n_in": {"position": {"x": 99, "y": 77}},
                "n_out": {"position": {"x": 500, "y": 300}},
            }
        }
        r = c.post(f"/agents/{AGENT_ID}/versions", json={"ir": ir("0.1.0", view=view_b)})
        check(
            "re-publish v0.1.0 with a moved view → idempotent 200 (NOT 409 version_conflict)",
            r.status_code == 200,
            f"status {r.status_code}",
        )
        h2 = next(v["content_hash"] for v in r.json()["versions"] if v["version"] == "0.1.0")
        check("drag leaves contentHash UNCHANGED (server-verified)", h2 == h1, f"{h2} == {h1}")

        # 3. a STRUCTURAL edit (relabel a node) → hash MUST change
        r = c.post(
            f"/agents/{AGENT_ID}/versions",
            json={"ir": ir("0.2.0", view=view_a, in_label="start")},
        )
        check(
            "add v0.2.0 (structural edit) → 200/201",
            r.status_code in (200, 201),
            f"status {r.status_code}",
        )
        h3 = next(v["content_hash"] for v in r.json()["versions"] if v["version"] == "0.2.0")
        check("a content edit CHANGES contentHash", h3 != h1, f"{h3} != {h1}")

        # 4. reload — the stored IR round-trips (view-stripped, contentHash stamped)
        r = c.get(f"/agents/{AGENT_ID}/versions/0.2.0")
        check("reload v0.2.0 → 200", r.status_code == 200, f"status {r.status_code}")
        stored = r.json()
        check("reloaded IR carries the server contentHash", stored["ir"].get("contentHash") == h3)
        check(
            "reloaded IR is view-stripped (layout stored separately)",
            stored["ir"].get("view") in (None, {}),
        )
        check("reloaded node label round-tripped", stored["ir"]["nodes"][0].get("label") == "start")

        # 5. execute via the agent run path — the canvas changed nothing the runtime sees.
        run_id = run_agent(c, AGENT_ID, "0.2.0", "hello-from-smoke")
        check("run produced a run id (streamed)", bool(run_id), run_id or "")
        # poll the persisted Run for its terminal outcome (run output is persisted to the DB).
        outcome = poll_run(c, run_id)
        check(
            "run completed via the walker",
            outcome["status"] == "completed",
            f"status {outcome['status']}",
        )
        check(
            "output == input (input→output passthrough)",
            outcome["output"] == "hello-from-smoke",
            repr(outcome["output"]),
        )

    print(f"\nALL {_passed} CHECKS PASSED — drag-doesn't-hash + content-does + runs unchanged.")


def run_agent(c: httpx.Client, agent_id: str, version: str, value: str) -> str:
    """Invoke /agents/{id}/runs (by agent reference) and read the run id off the SSE stream."""
    run_id = ""
    with c.stream(
        "POST",
        f"/agents/{agent_id}/runs",
        json={"input": value, "version": version},
    ) as r:
        if r.status_code != 200:
            r.read()
            print(f"\nrun POST → {r.status_code}: {r.text}", file=sys.stderr)
            sys.exit(1)
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            try:
                frame = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict) and frame.get("runId") and not run_id:
                run_id = frame["runId"]
    return run_id


def poll_run(c: httpx.Client, run_id: str, *, tries: int = 40) -> dict[str, Any]:
    for _ in range(tries):
        r = c.get(f"/runs/{run_id}")
        if r.status_code == 200:
            run = r.json()
            if run["status"] in ("completed", "failed"):
                return run
        time.sleep(0.25)
    print(f"\nrun {run_id} never terminalized", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
