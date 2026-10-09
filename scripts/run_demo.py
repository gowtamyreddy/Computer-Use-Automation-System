"""End-to-end demo that produces /evidence.

    python scripts/run_demo.py                      # real LLM discovery (needs OPENAI_API_KEY) + all replays
    python scripts/run_demo.py --artifact PATH      # skip discovery, replay an existing artifact

Steps: start two CoreLink tenants (base :8765, lakeside :8766) -> discovery (gpt) records capabilities ->
replay matrix (happy path, business outcomes, recoverable conditions, hard failure, live human handoff,
tenant variant with/without overlay) -> copy every run's evidence into evidence/ with a SUMMARY.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EVIDENCE = ROOT / "evidence"
PY = sys.executable

from cua.config import load_dotenv  # noqa: E402
from scripts import operator_bot as bot  # noqa: E402

BALANCE_GOAL = "Look up member {member_id} and read their current Share Savings balance"
SUBACCT_GOAL = ("For member {member_id}, open a new {share_type} sub-account with nickname {nickname} and initial deposit "
                "{deposit}, and reach the confirmation screen. Do not confirm it.")


def cua(*args: str, check: bool = False) -> tuple[int, str, str]:
    p = subprocess.run([PY, "-m", "cua.cli", *args], cwd=ROOT, capture_output=True, text=True)
    sys.stderr.write(p.stderr[-4000:])
    if check and p.returncode != 0:
        raise SystemExit(f"cua {' '.join(args)} failed:\n{p.stdout}\n{p.stderr}")
    return p.returncode, p.stdout, p.stderr


def run_dir(stderr: str) -> Path:
    line = [l for l in stderr.splitlines() if l.startswith("Evidence: ")][-1]
    return ROOT / line.split("Evidence: ", 1)[1].strip()


def start_app(port: int, variant: str) -> subprocess.Popen:
    p = subprocess.Popen([PY, "-m", "target_app.server", "--port", str(port), "--variant", variant], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=1)
            return p
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"app on {port} did not start")


def faults(tenant: str, *kv: str) -> None:
    cua("faults", "--tenant", tenant, "--reset", *kv, check=True)


def keep(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", help="skip discovery and replay this artifact")
    ap.add_argument("--skip-subaccount", action="store_true")
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    os.environ.setdefault("CUA_SECRET_DEMO_CORELINK_USERNAME", "teller1")
    os.environ.setdefault("CUA_SECRET_DEMO_CORELINK_PASSWORD", "demo-only-not-a-secret")
    os.environ.setdefault("CUA_SECRET_LAKESIDE_CORELINK_USERNAME", "teller1")
    os.environ.setdefault("CUA_SECRET_LAKESIDE_CORELINK_PASSWORD", "demo-only-not-a-secret")
    apps = [start_app(8765, "base"), start_app(8766, "lakeside")]
    summary: list[tuple[str, str, str, str]] = []
    try:
        (EVIDENCE / "discovery").mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "replays").mkdir(parents=True, exist_ok=True)
        if a.artifact:
            artifact = ROOT / a.artifact
        else:
            if not os.environ.get("OPENAI_API_KEY"):
                raise SystemExit("OPENAI_API_KEY is not set (put it in .env) -- or pass --artifact to skip discovery")
            faults("demo")
            code, out, err = cua("discover", "--no-operator", "--goal", BALANCE_GOAL,
                                 "--param", "member_id=12345::5-digit member number",
                                 "--capability-id", "corelink.member.read_savings_balance")
            res = json.loads(out)
            keep(run_dir(err), EVIDENCE / "discovery" / "read_savings_balance")
            summary.append(("discovery: read savings balance", "demo", "success" if res["success"] else "FAILED", res.get("stop_code") or ""))
            if not res["success"]:
                raise SystemExit(f"discovery failed: {res}")
            artifact = ROOT / res["artifact"]
            if not a.skip_subaccount:
                code, out, err = cua("discover", "--no-operator", "--goal", SUBACCT_GOAL,
                                     "--param", "member_id=23456::5-digit member number",
                                     "--param", "share_type=Money Market::share product to open",
                                     "--param", "nickname=Rainy Day::nickname for the new share",
                                     "--param", "deposit=250.00::initial deposit amount",
                                     "--capability-id", "corelink.member.open_subaccount_to_review")
                res2 = json.loads(out)
                keep(run_dir(err), EVIDENCE / "discovery" / "open_subaccount_to_review")
                summary.append(("discovery: open sub-account (stop at review)", "demo", "success" if res2["success"] else "FAILED", res2.get("stop_code") or ""))
                if res2["success"]:
                    shutil.copy(ROOT / res2["artifact"], EVIDENCE / "artifact_open_subaccount_to_review.yaml")
                    faults("demo")
                    code, out, err = cua("replay", res2["artifact"], "--param", "member_id=12345", "--param", "share_type=Share Certificate",
                                         "--param", "nickname=College", "--param", "deposit=1000")
                    keep(run_dir(err), EVIDENCE / "replays" / "subaccount-to-review")
                    r = json.loads(out)
                    summary.append(("replay sub-account to review (different inputs)", "demo", r["status"], (r.get("error") or {}).get("code", "")))
        shutil.copy(artifact, EVIDENCE / "artifact_read_savings_balance.yaml")
        rel = str(artifact.relative_to(ROOT))

        scenarios = [
            ("happy-path", "demo", ["member_id=12345"], []),
            ("happy-path-other-member", "demo", ["member_id=23456"], []),
            ("business-not-found", "demo", ["member_id=99999"], []),
            ("business-access-denied", "demo", ["member_id=55555"], []),
            ("business-validation", "demo", ["member_id=1234"], []),
            ("recover-interstitial", "demo", ["member_id=12345"], ["notice_once=1"]),
            ("recover-busy-retry", "demo", ["member_id=12345"], ["busy_once=1"]),
            ("recover-session-expired", "demo", ["member_id=12345"], ["expire_after=6"]),
            ("recover-hold-dialog", "demo", ["member_id=77777"], []),
            ("slow-host", "demo", ["member_id=12345"], ["slow_ms=2000"]),
            ("hard-failure-app-error", "demo", ["member_id=12345"], ["error_once=1"]),
            ("rejected-missing-input", "demo", [], []),
            ("tenant-lakeside-with-overlay", "lakeside", ["member_id=12345"], []),
            ("tenant-lakeside-no-overlay-degrades", "lakeside-unmapped", ["member_id=12345"], []),
        ]
        for name, tenant, params, fl in scenarios:
            faults("lakeside" if tenant.startswith("lakeside") else "demo", *fl)
            args = ["replay", rel, "--tenant", tenant, "--verbose"]
            for p in params:
                args += ["--param", p]
            code, out, err = cua(*args)
            r = json.loads(out)
            keep(run_dir(err), EVIDENCE / "replays" / name)
            detail = (r.get("outcome") or {}).get("code") or (r.get("error") or {}).get("code") or ""
            extra = [x["code"] for x in r["recoveries"]] + [f"{w['code']}@{w['step_id']}" for w in r["warnings"]]
            summary.append((f"replay {name}", tenant, r["status"], " ".join(filter(None, [detail, *extra]))))

        # Live handoff: host crashes mid-flow, operator takes over the same browser over CDP, automation resumes.
        faults("demo", "error_once=1")
        def operator():
            bot.wait_for_intervention("http://127.0.0.1:8780", 60)
            bot.claim("http://127.0.0.1:8780", "op.alice")
            bot.on_live_page(9222, bot.lookup_member("12345"))
            bot.release("http://127.0.0.1:8780", "skip_step", "host crashed on submit; re-ran the inquiry by hand")
        t = threading.Thread(target=operator, daemon=True)
        t.start()
        code, out, err = cua("replay", rel, "--param", "member_id=12345", "--escalate", "--debug-port", "9222", "--verbose")
        t.join(10)
        r = json.loads(out)
        keep(run_dir(err), EVIDENCE / "replays" / "handoff-human-takes-over")
        summary.append(("replay handoff-human-takes-over", "demo", r["status"],
                        f"intervention={r['interventions'][0]['code']}->{r['interventions'][0]['resolution']} "
                        f"human_actions={len(r['interventions'][0]['human_actions'])}" if r["interventions"] else ""))
        faults("demo")
    finally:
        for p in apps:
            p.terminate()

    lines = ["# Evidence summary", "", "Generated by `python scripts/run_demo.py`. Each row links to a run directory",
             "(`events.jsonl`, `result.json`, masked `screens/`, redacted `dom/`, `interventions/`, and for discovery",
             "`llm_transcript.jsonl` + `artifact.yaml`).", "", "| run | tenant | status | code / recoveries / warnings |", "|---|---|---|---|"]
    lines += [f"| {n} | {t} | {s} | {d} |" for n, t, s, d in summary]
    (EVIDENCE / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
