"""Score a run's predictions with the official SWE-bench Pro harness.

Runs SWE-bench_Pro-os `swe_bench_pro_eval.py` (cloned by setup.sh) with local
Docker and the `jefzda` images, on the V1 dataset rows of the predicted
instances. Writes runs/<run_id>/evaluation/<timestamp>/ and prints the resolved
count.

    uv run python evaluate.py --run-id <run_id> --workers 8
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

from swepro import dataset

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "external" / "SWE-bench_Pro-os"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--redo", action="store_true", help="re-run instances the harness already scored")
    args = parser.parse_args()

    if not (HARNESS / "swe_bench_pro_eval.py").exists():
        raise SystemExit("No SWE-bench_Pro-os checkout: run ./setup.sh first")
    run_dir = HERE / "runs" / args.run_id
    predictions_file = run_dir / "predictions.jsonl"
    predictions = [json.loads(line) for line in predictions_file.read_text().splitlines() if line.strip()] \
        if predictions_file.exists() else []
    if not predictions:
        raise SystemExit(f"No predictions in {run_dir}; run run.py first")
    run = json.loads((run_dir / "run.json").read_text())
    if run["dataset"] != dataset.identity():
        raise SystemExit(f"The run used {run['dataset']}, this harness reads {dataset.identity()}")

    out_dir = run_dir / "evaluation" / time.strftime("%Y%m%dT%H%M%S")
    out_dir.mkdir(parents=True)
    patches = [{"instance_id": p["instance_id"], "patch": p["prediction"], "prefix": p["model"]} for p in predictions]
    (out_dir / "patches.json").write_text(json.dumps(patches, indent=2))
    with open(out_dir / "raw_samples.jsonl", "w") as f:
        for p in predictions:
            f.write(json.dumps(dataset.get_instance(p["instance_id"]), default=str) + "\n")
    cmd = [sys.executable, "swe_bench_pro_eval.py",
           "--raw_sample_path", str(out_dir / "raw_samples.jsonl"),
           "--patch_path", str(out_dir / "patches.json"),
           "--output_dir", str(out_dir),
           "--scripts_dir", str(HARNESS / "run_scripts"),
           "--num_workers", str(args.workers),
           "--dockerhub_username", dataset.DOCKERHUB_USERNAME,
           "--use_local_docker"] + (["--redo"] if args.redo else [])
    print(" ".join(cmd), flush=True)
    with open(out_dir / "harness.log", "w") as log:
        subprocess.run(cmd, cwd=HARNESS, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    results_path = out_dir / "eval_results.json"
    if not results_path.exists():
        raise SystemExit(f"The harness wrote no eval_results.json; see {out_dir / 'harness.log'}")
    results = json.loads(results_path.read_text())
    resolved = {p["instance_id"]: bool(results.get(p["instance_id"])) for p in predictions}
    summary = {
        "run_id": args.run_id,
        "evaluated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "instances": len(resolved),
        "resolved": sum(resolved.values()),
        "resolve_rate": round(100 * sum(resolved.values()) / len(resolved), 2),
        "empty_patches": sum(1 for p in predictions if not p["prediction"].strip()),
        "per_instance": resolved,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Resolved {summary['resolved']}/{summary['instances']} ({summary['resolve_rate']}%); {out_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
