"""Run iCode (headless `icode run`) on SWE-bench Pro instances.

Each instance runs in its own fresh container (swepro/container.py): iCode, from
the self-contained runtime that setup.sh builds out of this checkout (runtime/
for glibc images, runtime-musl/ for Alpine ones), is mounted read-only at
/opt/icode and run on the repository at /app. The images themselves are never
modified. The container's only
way out is the gateway (swepro/gateway.py), which holds the API key on the host.
Outputs go to runs/<run_id>/: run.json, console.log (what this command prints,
appended on every invocation), predictions.jsonl and, per instance,
task.md, the iCode progress log and final answer, raw.diff, prediction.patch,
iCode's session and trajectory (chrys/) and result.json.

A rerun with the same run id skips instances that finished, `completed` or
`timeout` (a timed-out instance keeps the patch it had, as in the official
protocol), and retries the others, which failed on infrastructure (--redo
reruns everything). Every attempt starts from the clean base commit. A run id
only takes more instances under the configuration it was created with (model
profile, prompt, timeout, iCode runtimes, this harness's code): a different one
is refused, so a run never mixes configurations. When only the harness's code
changed, --allow-code-change continues the run and records the change.

    export ANTHROPIC_API_KEY=...   # or put it in .env next to this file
    uv run python run.py --run-id smoke --instance-id <instance_id>
    uv run python run.py --run-id main --instances instance_lists/pro_main_200.txt --workers 4
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import yaml

from swepro import container, dataset
from swepro.gateway import Gateway

HERE = Path(__file__).resolve().parent
ICODE_ROOT = HERE.parents[1]
RUNTIMES = {"glibc": HERE / "runtime", "musl": HERE / "runtime-musl"}
RUNS = HERE / "runs"
MOUNT = "/opt/icode"
TASK_PATH = "/tmp/icode-task.md"
LAUNCH = [f"{MOUNT}/python/bin/python3.14", "-I", "-c",
          "import sys; sys.argv[0] = 'icode'; from chrys.app.cli.app import main; sys.exit(main())"]
# A spec block of the prompt template: kept for a row with Requirements and
# Interface (every Pro row), marker lines dropped.
SPEC_BLOCK = re.compile(r"^<!-- spec -->\n(.*?)(?:^<!-- no spec -->\n(.*?))?^<!-- end spec -->\n", re.M | re.S)
# Instance outcomes a rerun keeps; anything else is retried.
FINAL_STATUSES = ("completed", "timeout")
# `timeout` exits 124 (GNU, glibc images) or 143 (busybox, Alpine images) when
# it stops iCode, 137 when it has to kill it.
TIMEOUT_EXITS = (124, 137, 143)
# What decides the agent's behaviour, and must match for a run id to take more instances.
CONFIG_KEYS = ("system", "runtimes", "harness_sha256", "model_profile", "prompt_sha256", "timeout_min",
               "dataset", "network")
# The run's own model profile is compared as `model_profile`; the other profiles
# in config/models/ do not affect it, so adding one leaves existing runs open.
HARNESS_FILES = ("run.py", "setup.sh", "swepro/*.py", "config/prompt.md")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    env_file = HERE / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "ANTHROPIC_API_KEY":
                key = value.strip().strip("'\"")
    if not key:
        raise SystemExit(f"Set ANTHROPIC_API_KEY in the environment or in {env_file}")
    return key


def render_prompt(instance: dict) -> str:
    """config/prompt.md is swebench-pro-cli-runner's fix_agent/main.txt (commit
    9169749), rendered as that runner renders it for a Pro row
    (agents/fixer/prompt.py, no features enabled), with the repository at /app."""
    template = SPEC_BLOCK.sub(lambda block: block.group(1), (HERE / "config" / "prompt.md").read_text())
    return template.format(
        repo_name=instance["repo"],
        issue_description=dataset.normalize(instance.get("problem_statement", "")),
        requirements=dataset.normalize(instance.get("requirements", "")),
        interface=dataset.normalize(instance.get("interface", "")),
        base_path=dataset.REPO_DIR,
    )


def load_profile(name: str) -> dict:
    for path in sorted((HERE / "config" / "models").glob("*.yaml")):
        profile = yaml.safe_load(path.read_text())
        if profile.get("name") == name:
            return profile
    raise SystemExit(f"No model profile named {name!r} in config/models/")


def harness_sha256() -> str:
    digest = hashlib.sha256()
    files = sorted({p for pattern in HARNESS_FILES for p in HERE.glob(pattern) if p.is_file()})
    for path in files:
        digest.update(str(path.relative_to(HERE)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def run_info(args, profile: dict) -> dict:
    def git(*a):
        return subprocess.run(["git", "-C", str(ICODE_ROOT), *a], capture_output=True, text=True).stdout.strip()

    builds = {libc: path / "BUILD_INFO.json" for libc, path in RUNTIMES.items()}
    return {
        "run_id": args.run_id, "created": now(),
        "system": f"iCode `icode run -a {args.agent}` inside the instance container",
        "runtimes": {libc: json.loads(b.read_text()) if b.exists() else None for libc, b in builds.items()},
        "harness_sha256": harness_sha256(),
        "harness_checkout": {"commit": git("rev-parse", "HEAD"),
                             "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))},
        "model_profile": {k: v for k, v in profile.items() if k not in ("api_key", "base_url")},
        "prompt_sha256": hashlib.sha256((HERE / "config" / "prompt.md").read_bytes()).hexdigest(),
        "timeout_min": args.timeout_min,
        "dataset": dataset.identity(),
        "network": f"docker internal network {container.NETWORK}; only the host-side Anthropic gateway is reachable",
    }


def run_instance(args, instance_id: str, gateway: Gateway, profile: dict) -> dict:
    inst_dir = RUNS / args.run_id / "instances" / instance_id
    previous = inst_dir / "result.json"
    if previous.exists() and not args.redo:
        status = json.loads(previous.read_text()).get("status")
        if status in FINAL_STATUSES:
            return {"instance_id": instance_id, "status": f"skipped ({status})"}
    shutil.rmtree(inst_dir, ignore_errors=True)
    inst_dir.mkdir(parents=True)

    result = {"instance_id": instance_id, "started": now(), "status": "failed", "stages": {}}
    name = "icode-swepro-" + hashlib.sha1(f"{args.run_id}|{instance_id}".encode()).hexdigest()[:12]
    t_total = time.time()
    try:
        instance = dataset.get_instance(instance_id)
        image = dataset.image(instance)
        result["image"] = image
        container.ensure_image(image)

        t0 = time.time()
        libc = container.image_libc(image)
        runtime = RUNTIMES[libc]
        if not (runtime / "python" / "bin" / "python3.14").exists():
            raise RuntimeError(f"{libc} image, but {runtime.name}/ is not built: run ./setup.sh")
        result["libc"] = libc
        container.start(name, image, {str(runtime): MOUNT})
        container.prepare_repo(name, instance)
        result["stages"]["prepare_s"] = round(time.time() - t0, 1)

        (inst_dir / "task.md").write_text(render_prompt(instance))
        home = container.check(container.exec_sh(name, 'printf %s "$HOME"'), "read HOME") or "/root"
        result["home"] = home
        with tempfile.TemporaryDirectory() as config_dir:
            models = Path(config_dir) / "models"
            models.mkdir()
            filled = dict(profile, base_url=gateway.base_url, api_key=gateway.token)
            (models / f"{profile['id']}.yaml").write_text(yaml.safe_dump(filled, sort_keys=False))
            container.check(container.run(["docker", "cp", f"{config_dir}/.", f"{name}:{home}/.chrys"]), "copy config")
        container.check(container.run(["docker", "cp", str(inst_dir / "task.md"), f"{name}:{TASK_PATH}"]), "copy task")

        t0 = time.time()
        cmd = ["docker", "exec", "--user", "root", "-w", dataset.REPO_DIR,
               "-e", f"NO_PROXY={gateway.listen_host}", name,
               "timeout", "-k", "30", str(args.timeout_min * 60),
               *LAUNCH, "run", "--task", TASK_PATH, "-a", args.agent, "-m", profile["name"], "-C", dataset.REPO_DIR]
        with open(inst_dir / "icode.stdout.md", "w") as out, open(inst_dir / "icode.progress.log", "w") as err:
            proc = subprocess.run(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
        result["icode_exit"] = proc.returncode
        result["stages"]["icode_s"] = round(time.time() - t0, 1)

        raw, patch = container.capture_patch(name, instance)
        (inst_dir / "raw.diff").write_text(raw)
        (inst_dir / "prediction.patch").write_text(patch)
        result["patch_lines"] = len(patch.splitlines())
        if proc.returncode == 0:
            result["status"] = "completed"
        elif proc.returncode in TIMEOUT_EXITS or result["stages"]["icode_s"] >= args.timeout_min * 60 - 5:
            result["status"] = "timeout"
        else:
            result["status"] = f"icode_exit_{proc.returncode}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
    finally:
        container.run(["docker", "cp", f"{name}:{result.get('home', '/root')}/.chrys", str(inst_dir / "chrys")])
        if not args.keep_container:
            container.remove(name)
        result["finished"] = now()
        result["stages"]["total_s"] = round(time.time() - t_total, 1)
        write_json(inst_dir / "result.json", result)
    return result


def write_predictions(run_dir: Path) -> int:
    rows = []
    for inst_dir in sorted((run_dir / "instances").iterdir()):
        patch_file = inst_dir / "prediction.patch"
        if patch_file.exists():
            patch = patch_file.read_text()
            rows.append({"instance_id": inst_dir.name, "model_name_or_path": run_dir.name,
                         "model_patch": patch, "model": run_dir.name, "prediction": patch})
    with open(run_dir / "predictions.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--instance-id", action="append", help="repeatable")
    which.add_argument("--instances", help="file with one instance id per line")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--model", default="haiku-4.5", help="model profile name in config/models/")
    parser.add_argument("--agent", default="Code")
    parser.add_argument("--timeout-min", type=int, default=30,
                        help="wall clock for `icode run` (swebench-pro-cli-runner's fixer: 1800 s)")
    parser.add_argument("--redo", action="store_true", help="also rerun instances that completed")
    parser.add_argument("--allow-code-change", action="store_true",
                        help="add instances to a run created by different harness code (only the code may differ; "
                             "recorded in run.json under code_changes)")
    parser.add_argument("--keep-container", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

    if not (RUNTIMES["glibc"] / "python" / "bin" / "python3.14").exists():
        raise SystemExit("No iCode runtime: run ./setup.sh first")
    ids = args.instance_id or dataset.read_instance_ids(args.instances)
    for iid in ids:  # unknown ids fail here, before any container starts
        dataset.get_instance(iid)
    profile = load_profile(args.model)
    key = api_key()
    run_dir = RUNS / args.run_id
    info = run_info(args, profile)
    if (run_dir / "run.json").exists():
        recorded = json.loads((run_dir / "run.json").read_text())
        changed = [k for k in CONFIG_KEYS if recorded.get(k) != info[k]]
        if changed == ["harness_sha256"] and args.allow_code_change:
            recorded.setdefault("code_changes", []).append(
                {"at": now(), "from": recorded["harness_sha256"], "to": info["harness_sha256"]})
            recorded["harness_sha256"] = info["harness_sha256"]
            write_json(run_dir / "run.json", recorded)
        elif changed:
            hint = (" Only this harness's code differs: --allow-code-change continues the run and records the "
                    "change in run.json." if changed == ["harness_sha256"] else "")
            raise SystemExit(f"{args.run_id} was created with a different configuration ({', '.join(changed)} "
                             "differ), so this command cannot add instances to it. Use a new --run-id, "
                             "or restore that configuration." + hint)
    else:
        (run_dir / "instances").mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "run.json", info)

    # Everything printed below, and logged warnings, also go to runs/<run_id>/console.log.
    console = open(run_dir / "console.log", "a", encoding="utf-8", buffering=1)
    handler = logging.FileHandler(run_dir / "console.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logging.getLogger().addHandler(handler)

    def say(line: str) -> None:
        print(line, flush=True)
        console.write(line + "\n")

    say(f"{now()} run {args.run_id}: {len(ids)} instance(s), model profile {profile['name']}, "
        f"{args.workers} worker(s)")
    gateway = Gateway(container.ensure_network(), key).start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_instance, args, iid, gateway, profile): iid for iid in ids}
            for future in concurrent.futures.as_completed(futures):
                r = future.result()
                say(f"{r['status']:<22} {r.get('patch_lines', '-'):>5} lines  "
                    f"{r.get('stages', {}).get('total_s', '-'):>7}s  {r['instance_id']}"
                    + (f"  {r['error']}" if r.get("error") else ""))
    finally:
        gateway.stop()
        count = write_predictions(run_dir)
        say(f"{now()} {count} predictions in {run_dir / 'predictions.jsonl'}"
            + (f"; gateway refused {gateway.denied} request(s)" if gateway.denied else ""))
        console.close()


if __name__ == "__main__":
    main()
