"""Create (or revoke) the API key the integration tests use, on a running platform.

    INSIGHTMON_IT_ADMIN_EMAIL=... INSIGHTMON_IT_ADMIN_PASSWORD=... \
        uv run python scripts/provision_platform.py --base-url http://127.0.0.1:8790

Signs in as the admin, picks the project (`--project-id`, else the first project the admin
administers that has a dataset with at least two ready dev examples), creates a key with scopes
`trace_ingest`, `score_write` and `experiment_write`, and prints shell `export` lines for
`INSIGHTMON_IT_BASE_URL`, `INSIGHTMON_IT_API_KEY`, `INSIGHTMON_IT_API_KEY_ID` and
`INSIGHTMON_IT_PROJECT_ID` (to `eval` in a shell: the key is shown once and never written to a
file). Diagnostics go to stderr.

`--revoke <key id>` (with `--project-id`) revokes such a key instead: scripts/integration.sh
does it when the tests end, so a run against a shared platform leaves no usable key behind.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.integration.platform_api import AdminSession, PlatformError, responds

SCOPES = ["trace_ingest", "score_write", "experiment_write"]


def say(text: str) -> None:
    print(f"provision: {text}", file=sys.stderr, flush=True)


def pick_project(admin: AdminSession, wanted: str | None) -> str:
    projects = admin.projects()
    if wanted:
        if not any(p["id"] == wanted for p in projects):
            raise PlatformError(f"project {wanted} is not visible to the admin")
        return wanted
    for project in projects:
        if project.get("archived_at") or project.get("role") != "admin":
            continue
        admin.project_id = project["id"]
        try:
            admin.find_dataset(min_ready=2)
        except PlatformError:
            continue
        return str(project["id"])
    raise PlatformError("no project with a dataset of 2 ready dev examples (seed the demo data)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base-url", default=os.environ.get("INSIGHTMON_IT_BASE_URL"))
    parser.add_argument("--project-id", default=os.environ.get("INSIGHTMON_IT_PROJECT_ID"))
    parser.add_argument("--wait-s", type=float, default=60.0, help="wait for /healthz")
    parser.add_argument("--revoke", metavar="KEY_ID", help="revoke this key and exit")
    args = parser.parse_args()
    email = os.environ.get("INSIGHTMON_IT_ADMIN_EMAIL")
    password = os.environ.get("INSIGHTMON_IT_ADMIN_PASSWORD")
    if not args.base_url or not email or not password:
        say("needs --base-url, INSIGHTMON_IT_ADMIN_EMAIL and INSIGHTMON_IT_ADMIN_PASSWORD")
        return 2
    base_url = args.base_url.rstrip("/")
    deadline = time.monotonic() + args.wait_s
    while not responds(base_url + "/healthz"):
        if time.monotonic() > deadline:
            say(f"{base_url} does not answer /healthz")
            return 1
        time.sleep(1)

    admin = AdminSession(base_url, email, password)
    if args.revoke:
        if not args.project_id:
            say("--revoke needs --project-id (or INSIGHTMON_IT_PROJECT_ID)")
            return 2
        try:
            admin.project_id = args.project_id
            admin.revoke_api_key(args.revoke)
        except PlatformError as exc:
            say(f"could not revoke key {args.revoke}: {exc}")
            return 1
        finally:
            admin.close()
        say(f"project {args.project_id}: key {args.revoke} revoked")
        return 0
    try:
        admin.project_id = pick_project(admin, args.project_id)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        created = admin.create_api_key(f"sdk integration {stamp}", SCOPES)
    except PlatformError as exc:
        say(str(exc))
        return 1
    finally:
        admin.close()
    say(f"project {admin.project_id}: key {created['api_key']['id']} ({', '.join(SCOPES)})")
    print(f"export INSIGHTMON_IT_BASE_URL={shlex.quote(base_url)}")
    print(f"export INSIGHTMON_IT_API_KEY={shlex.quote(created['secret'])}")
    print(f"export INSIGHTMON_IT_API_KEY_ID={shlex.quote(str(created['api_key']['id']))}")
    print(f"export INSIGHTMON_IT_PROJECT_ID={shlex.quote(str(admin.project_id))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
