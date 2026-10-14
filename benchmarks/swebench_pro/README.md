# iCode on SWE-bench Pro

Runs iCode's headless `icode run` (built-in Code agent) on SWE-bench Pro V1
instances and scores the patches with the official harness. Nothing here changes
iCode's code: iCode is installed from this checkout, unmodified, and configured
only through a normal model profile.

## Setup

Needs Linux x86_64 with Docker, `uv` and `git`.

```bash
cd benchmarks/swebench_pro
./setup.sh                          # rerun after changing iCode's code
export ANTHROPIC_API_KEY=...        # or ANTHROPIC_API_KEY=... in benchmarks/swebench_pro/.env
```

`setup.sh` builds two iCode runtimes, each CPython from iCode's `.python-version`
with iCode and its locked dependencies: `runtime/` for the standard (glibc)
images and `runtime-musl/` for the 25 Alpine (musl) ones among the 200. It also
clones the official harness into `external/` and syncs this directory's
environment. The musl build is checked to contain no glibc-linked code and is
patched inside an `alpine` container. All of these, and `runs/`, are gitignored.

## Run, score, cost

```bash
uv run python run.py --run-id smoke --instance-id <instance_id>
uv run python run.py --run-id main --instances instance_lists/pro_main_200.txt --workers 4
uv run python evaluate.py --run-id main --workers 8
uv run python cost_report.py --run-id main
```

Rerunning a run id skips the instances that finished: `completed`, or `timeout`
(the 30-minute limit hit; the patch made so far is kept and graded, as in the
official protocol). Instances that failed on infrastructure are retried, and
`--redo` reruns everything. A run id only takes more instances under the
configuration it was created with (model profile, prompt, timeout, iCode
runtimes, this harness's code), so a pilot and a later full run can share one
run id and the pilot's instances are not paid for twice:

```bash
uv run python run.py --run-id haiku45 --instances instance_lists/pilot_6.txt --workers 6
uv run python run.py --run-id haiku45 --instances instance_lists/pro_first_100.txt --workers 6   # skips the 6
```

If only this harness's code changed in between (not the profile, prompt, timeout or iCode build), add
`--allow-code-change`: the run continues and `run.json` records the change. Prefix commands with
`HF_HUB_OFFLINE=1` to read the dataset from the local cache without contacting the Hugging Face Hub.

Each run starts a fresh container from the instance image at the clean base
commit and removes it afterwards, so neither the image nor the host changes.

Outputs, in `runs/<run_id>/`:

| Path | What |
|---|---|
| `run.json` | iCode version and commit, model profile, prompt hash, dataset revision; `code_changes` lists any harness-code change accepted with `--allow-code-change` |
| `console.log` | everything `run.py` printed, appended on each invocation (no `tee` needed) |
| `predictions.jsonl` | one row per instance, readable by both SWE-bench harnesses |
| `instances/<id>/task.md` | the prompt iCode received |
| `instances/<id>/icode.progress.log`, `icode.stdout.md` | `icode run` progress and final answer |
| `instances/<id>/raw.diff`, `prediction.patch` | the diff against the base commit, and the same without agent artifacts |
| `instances/<id>/chrys/` | iCode's config dir after the run: session, trajectory (`sessions/*/trajectory/events.jsonl`) |
| `instances/<id>/result.json` | status, exit code, timings, errors |
| `evaluation/<timestamp>/` | harness output and `summary.json` |
| `cost.json` | tokens and USD per instance |

## How a run is isolated

- **Repository.** The instance image is a full clone that carries the fix
  commit. Before iCode starts, the runner resets `/app` to the base commit the
  way the harness does, removes the remotes and prunes every commit outside the
  base commit's ancestry. It then checks that the fix commit is gone.
- **Network.** The container sits on the internal Docker network
  `icode-isolated`, which has no route out.
- **API key.** The key stays in the runner on the host. `swepro/gateway.py`
  listens on the network's gateway address. iCode's profile points `base_url`
  at the gateway and uses a random per-run token as its `api_key`. The gateway
  accepts only `POST /v1/messages*` with that token, swaps in the real key and
  streams the call to `api.anthropic.com`. The agent can find the token, which
  sits in the profile inside the container, but never the key, and the token is
  useless once the run ends.
- **iCode itself.** iCode runs from `runtime/` (or `runtime-musl/` on an Alpine
  image), mounted read-only at `/opt/icode`, in isolated mode (`python -I`). The
  repository's own Python and packages are untouched, and the images are never
  modified, so the evaluator grades in the original images.

## Configuration

- `config/models/*.yaml`: iCode model profiles, selected with `--model <name>`. Each matches what Claude Code
  2.1.273 and 2.1.284 send with swebench-pro-cli-runner's flags and `--effort high` (captured through a logging
  proxy); prompt caching is on in both:

  | Profile | Model | `max_tokens` | Thinking | Effort | Context window | Captured |
  |---|---|---|---|---|---|---|
  | `haiku-4.5` (default) | `claude-haiku-4-5-20251001` | 32,000 | enabled, budget 31,999, interleaved | none (Haiku 4.5 has no effort setting) | 200k | 2026-10-10 |
  | `opus-4.8` | `claude-opus-4-8` | 64,000 | adaptive | `high` | 200k | 2026-10-13 |

  Both keep every thinking block (`context_management` `clear_thinking` with `keep: all`). Use a new run id per
  model, for example `--run-id opus48 --model opus-4.8`.
- `config/prompt.md`: the task prompt, a byte-for-byte copy of
  swebench-pro-cli-runner's `fix_agent/main.txt` (commit `9169749`), rendered
  the way that runner renders it for a Pro instance (no features enabled) with
  the repository at `/app`. The result matches the runner's output for all 200
  instances in `pro_main_200.txt`.
- `instance_lists/`: instance id files. `pro_main_200.txt` is the 200-instance
  main set.

## Known limits

- The containers can also reach other services that listen on all interfaces of
  the host (through the network's gateway address).
- The gateway prices nothing and caps nothing; `--timeout-min` (default 30,
  the runner's 1800 s fixer limit) bounds each instance's wall time.
