"""The instance container iCode runs in.

SWE-bench Pro images are full clones that carry the fix commit, and the upstream
repository is one `git fetch` away. So each run

* starts a fresh container from the instance image on an internal Docker network
  (no route out; the host's gateway address is the only thing it can reach);
* resets the repository to the base commit exactly as the evaluation harness does
  (`git reset --hard` + `git checkout`, not `before_repo_set_cmd`, whose last
  line checks the hidden tests out of the fix commit), removes the remotes and
  prunes every commit outside the base commit's ancestry, then checks all that;
* captures the prediction as `git diff <base commit>`, new files included;
* removes the container, so nothing the agent did survives the run.
"""

import fnmatch
import os
import re
import subprocess

from . import dataset

NETWORK = os.environ.get("SWEPRO_DOCKER_NETWORK", "icode-isolated")
NETWORK_SUBNET = "172.30.0.0/24"
PSEUDO_REFS = ("ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_HEAD", "AUTO_MERGE")
# Agent artifacts and dependency directories that never belong in a prediction
# (swebench-pro-cli-runner's list), plus iCode's own files.
ARTIFACT_PATTERNS = [
    "CLAUDE.md", ".claude", ".claude/*",
    "node_modules", "node_modules/*",
    "__pycache__", "__pycache__/*", "*.pyc", "*.pyo",
    "venv", "venv/*", ".venv", ".venv/*", "*.egg-info", "*.egg-info/*",
    "vendor", "vendor/*",
    ".env",
    ".chrys", ".chrys/*", ".agents", ".agents/*", "AGENTS.md",
]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kwargs)


def check(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed ({result.returncode}): {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout


def ensure_network() -> str:
    """Create the internal network if needed; return its gateway address on the host."""
    if run(["docker", "network", "inspect", NETWORK]).returncode != 0:
        result = run(["docker", "network", "create", "--internal", "--subnet", NETWORK_SUBNET, NETWORK])
        if result.returncode != 0 and run(["docker", "network", "inspect", NETWORK]).returncode != 0:
            raise RuntimeError(f"Could not create network {NETWORK}: {result.stderr}")
    info = check(run(["docker", "network", "inspect", NETWORK, "-f",
                      "{{.Internal}} {{range .IPAM.Config}}{{.Gateway}}{{end}}"]), "network inspect").split()
    if info[0] != "true":
        raise RuntimeError(f"Docker network {NETWORK} exists but is not internal")
    return info[1]


def ensure_image(image: str) -> None:
    if run(["docker", "image", "inspect", image]).returncode != 0:
        check(run(["docker", "pull", image]), f"docker pull {image}")


def start(name: str, image: str, mounts: dict[str, str]) -> None:
    run(["docker", "rm", "-f", name])
    volumes = [arg for host, target in mounts.items() for arg in ("-v", f"{host}:{target}:ro")]
    check(run(["docker", "run", "-d", "--name", name, "--network", NETWORK, "--user", "root",
               # `git log`/`git show` would otherwise open a pager and wait for a key.
               "-e", "PAGER=cat", "-e", "GIT_PAGER=cat",
               *volumes, "--entrypoint", "/bin/sh", image, "-c", "tail -f /dev/null"]), "docker run")


def remove(name: str) -> None:
    run(["docker", "rm", "-f", name])


def exec_sh(name: str, script: str, *, workdir: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec", "--user", "root"] + (["-w", workdir] if workdir else []) + [name, "/bin/sh", "-c", script]
    return run(cmd)


def image_libc(image: str) -> str:
    """`musl` for an Alpine image, `glibc` otherwise: decides which iCode runtime to mount."""
    out = check(run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-c",
                     "if [ -e /lib/ld-musl-x86_64.so.1 ]; then echo musl; else echo glibc; fi"]), "libc probe")
    return out.strip()


def prepare_repo(name: str, instance: dict) -> None:
    base = instance["base_commit"]
    script = "\n".join([
        "set -e",
        f"cd {dataset.REPO_DIR}",
        "git config --global --add safe.directory '*'",
        "git config gc.auto 0",
        f"git reset -q --hard {base}",
        f"git checkout -q {base}",
        # Untracked files the image already has never go into a prediction.
        "mkdir -p .git/info",
        "git ls-files -z --others --exclude-standard | tr '\\0' '\\n' | sed 's|^|/|' >> .git/info/exclude",
        # Every ref but the checked-out branch goes, then whatever only they reached.
        'gitdir="$(git rev-parse --git-dir)"',
        'branch="$(git symbolic-ref -q HEAD || true)"',
        'if [ -z "$branch" ]; then',
        "  branch=refs/heads/base",
        '  git update-ref "$branch" HEAD',
        '  git symbolic-ref HEAD "$branch"',
        "fi",
        'for remote in $(git remote); do git remote remove "$remote"; done',
        "git for-each-ref --format='delete %(refname)' | grep -vxF \"delete $branch\" | git update-ref --no-deref --stdin",
        'find "$gitdir/refs" "$gitdir/logs/refs" -type f ! -path "$gitdir/$branch" '
        '! -path "$gitdir/logs/$branch" -delete 2>/dev/null || true',
        "for ref in " + " ".join(PSEUDO_REFS) + '; do rm -f "$gitdir/$ref"; done',
        "git reflog expire --expire=now --all",
        "git gc -q --prune=now",
    ])
    check(exec_sh(name, script), "repository preparation")
    verify_repo(name, instance)


def verify_repo(name: str, instance: dict) -> None:
    fix = dataset.fix_commit(instance) or ""
    script = "\n".join([
        f"cd {dataset.REPO_DIR}",
        'echo "head=$(git rev-parse HEAD)"',
        'echo "remotes=$(git remote | wc -l)"',
        'echo "outside=$(git rev-list --all --not HEAD | wc -l)"',
        f'if [ -n "{fix}" ]; then git cat-file -e "{fix}^{{commit}}" 2>/dev/null && echo fix=present || echo fix=absent; fi',
        # Submodules are left as the image has them (one openlibrary image's
        # vendor/infogami already shows as modified on the unmodified image).
        'echo "dirty=$(git status --porcelain --ignore-submodules=all | wc -l)"',
    ])
    out = exec_sh(name, script).stdout
    facts = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    problems = []
    if facts.get("head") != instance["base_commit"]:
        problems.append(f"HEAD is {facts.get('head')}, not the base commit")
    if facts.get("remotes") != "0":
        problems.append("remotes left")
    if facts.get("outside") != "0":
        problems.append("commits outside the base commit's ancestry")
    if facts.get("fix") == "present":
        problems.append("fix commit still present")
    if facts.get("dirty") != "0":
        problems.append("working tree not clean")
    if problems:
        raise RuntimeError(f"Repository check failed: {problems}")


def _patch_path(header: str) -> str:
    match = re.match(r'diff --git (?:"?a/)(.+?)"? (?:"?b/)(.+?)"?$', header)
    return match.group(2) if match else header


def filter_patch(patch: str) -> str:
    """Drop agent artifacts and symlinks from a diff."""
    kept = []
    for chunk in re.split(r"(?m)^(?=diff --git )", patch or ""):
        if not chunk.startswith("diff --git "):
            continue
        header, _, body = chunk.partition("\n")
        path = _patch_path(header)
        if re.search(r"(?m)^(new|deleted) file mode 120000$", body):
            continue
        if any(fnmatch.fnmatch(path, pattern) for pattern in ARTIFACT_PATTERNS):
            continue
        kept.append(chunk if chunk.endswith("\n") else chunk + "\n")
    return "".join(kept)


def capture_patch(name: str, instance: dict) -> tuple[str, str]:
    """(raw diff, filtered prediction) against the base commit, new files included."""
    diff = (f"git add -N . && git -c core.fileMode=false diff {instance['base_commit']} "
            "--no-color --no-ext-diff --ignore-submodules=all")
    raw = check(exec_sh(name, diff, workdir=dataset.REPO_DIR), "git diff")
    return raw, filter_patch(raw)
