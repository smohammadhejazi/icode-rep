"""SWE-bench Pro V1: dataset rows, task text and instance images.

V1 is config `v1` of `ScaleAI/SWE-bench_Pro` (731 tasks, images
`jefzda/sweap-images:*`, repository at /app). On 2026-09-22 the dataset's
default config became V2 (642 tasks, new images); V1 stayed byte-identical as
config `v1`, pinned here to the revision that added it.
"""

import json
import re
from functools import cache

DATASET = "ScaleAI/SWE-bench_Pro"
CONFIG = "v1"
REVISION = "2d52cb3df914a3fcf80c7f66738b3a88ae37fc50"
DOCKERHUB_USERNAME = "jefzda"
REPO_DIR = "/app"


def identity() -> dict:
    return {"dataset": DATASET, "config": CONFIG, "revision": REVISION}


@cache
def _rows() -> dict[str, dict]:
    from datasets import load_dataset

    rows = load_dataset(DATASET, CONFIG, split="test", revision=REVISION)
    return {row["instance_id"]: dict(row) for row in rows}


def get_instance(instance_id: str) -> dict:
    rows = _rows()
    if instance_id not in rows:
        raise ValueError(f"Not a SWE-bench Pro V1 instance: {instance_id}")
    return dict(rows[instance_id])


def read_instance_ids(path: str) -> list[str]:
    """One instance id per line; blank lines and '#' comments ignored."""
    ids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                ids.append(line)
    if len(ids) != len(set(ids)):
        raise SystemExit(f"{path} lists an instance more than once")
    return ids


def normalize(value) -> str:
    """Some fields are JSON-quoted string literals with escaped newlines."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                text = decoded
        except json.JSONDecodeError:
            pass
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    return text


def image(instance: dict) -> str:
    """Docker Hub image the instance is solved and scored in. Port of
    `helper_code/image_uri.py:get_dockerhub_image_uri` in SWE-bench_Pro-os."""
    instance_id, repo = instance["instance_id"], instance["repo"]
    repo_base, repo_name = repo.lower().split("/")
    tag_id = instance_id.replace("instance_", "")
    if instance_id == "instance_element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan":
        repo_name = "element-web"
    elif "element-hq" in repo.lower() and "element-web" in repo.lower():
        repo_name = "element"
        if tag_id.endswith("-vnan"):
            tag_id = tag_id[:-5]
    elif tag_id.endswith("-vnan"):
        tag_id = tag_id[:-5]
    tag = f"{repo_base}.{repo_name}-{tag_id}"[:128]
    return f"{DOCKERHUB_USERNAME}/sweap-images:{tag}"


def fix_commit(instance: dict) -> str | None:
    """The commit that fixed the task: the one the evaluation checks the hidden
    tests out of (last line of `before_repo_set_cmd`)."""
    last = instance.get("before_repo_set_cmd", "").strip().splitlines()[-1]
    match = re.match(r"git checkout ([0-9a-f]{40}) -- ", last.strip())
    return match.group(1) if match else None
