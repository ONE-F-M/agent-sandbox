"""dev_agent_server.py

Generic, config-free driver for the dev-agent sandbox container.

This process IS the coding agent. It carries no system prompt, no model
choice, no tool list, and no credential of its own — every one of those
arrives fresh in the POST /run body's "agent_config" (system_prompt, model,
api_key) and "tools" (Anthropic-format schemas), resolved by Processa on
every dispatch: agent_config from the "Dev Agent" AI Agent Configuration and
its linked AI Model, tools from the shapes of the BPMN map's own
"sandbox_tool_defs" ad-hoc sub-process (see one_bpmn's
agent_sandbox_ops.py::dispatch and api/compilation.py::_resolve_sandbox_tool_shapes).
This process never invents behavior, a tool set, or a credential the caller
didn't supply — only WHICH tools exist is decided elsewhere; EXECUTING each
one, against the model it was told to use, deciding what to read, what to
change, and when it's done, all still happens entirely here.

Given a work order (target_app, git_branch, work_item_description), that
agent_config, and that tools list, it runs its own bounded tool-calling loop.
Processa never sees the file plan in advance; it only gets the sandbox's own
turn-by-turn trace and the actual diff, in the callback.

Stdlib plus the anthropic SDK (installed at image-build time — see
Dockerfile.frappe_runtime; dev_agent_server.py runs via system python3, not
the bench's own venv, per entrypoint.sh).

PR-opening is one of the tools the model can choose to call (open_pull_request,
see _tool_open_pull_request below), not automatic post-loop logic — it opens
the pull request directly against GitHub's REST API (see _open_pr), the same
Contents-API mechanism api/github_sync.py's open_customization_pr() already
uses on the Processa side for other PR-opening flows, just run from here
instead. Processa's callback handler only ever records the result (the PR
URL, or why one wasn't opened); it never talks to GitHub for this itself.
The GitHub token arrives fresh per dispatch in the request body, exactly
like agent_config's model credential — never a static secret baked into
this container's own deployment.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

_GITHUB_API = "https://api.github.com"

BENCH_DIR = "/home/frappe/frappe-bench"
SITE_NAME = "sandbox"

# Sandbox-infra config, set per environment by deploy.py (SANDBOX_ENV,
# CALLBACK_HMAC_SECRET) or optionally by whoever operates this service
# (ALLOWED_CALLBACK_HOST). None of this is agent behavior — that arrives
# per-request in agent_config, from Processa.
SANDBOX_ENV = os.environ.get("SANDBOX_ENV", "local")
# .strip() is deliberate: a secret piped into `gcloud secrets create` from a
# command like `openssl rand -hex 32` (which prints a trailing newline) gets
# that newline stored as part of the secret's bytes, and Cloud Run injects it
# into the env var verbatim. A value copy-pasted into Processa Settings by a
# person doesn't carry that same trailing newline, so the two sides silently
# sign against different byte sequences — confirmed the hard way: every
# callback got rejected as a signature mismatch despite both sides holding
# what looked, and hashed, like the identical secret. Stripping here (and on
# the verification side in dev_agent_callback.py) makes the comparison
# robust to exactly this class of secret-creation whitespace accident.
CALLBACK_HMAC_SECRET = os.environ.get("CALLBACK_HMAC_SECRET", "").strip()
# Optional pin: when set, /run refuses any callback_url whose host doesn't
# match — keeps a beta sandbox from ever being pointed at a production
# Processa instance (or anywhere else) by a malformed/malicious payload.
ALLOWED_CALLBACK_HOST = os.environ.get("ALLOWED_CALLBACK_HOST", "")

CALLBACK_MAX_ATTEMPTS = 3
CALLBACK_RETRY_BACKOFF_SECONDS = 5

# Hard ceiling on the coding loop's own turns — independent of Processa's
# own aiMaxToolCalls (that bounds Dev Agent's outer, one-tool-call dispatch;
# this bounds the sandbox's inner read/write/test loop). Cloud Run's own
# request timeout (deploy.py's CLOUD_RUN_TIMEOUT, currently 3600s) is the
# outer backstop if a loop somehow both runs long AND keeps calling tools.
MAX_AGENT_ITERATIONS = 30
AGENT_MAX_TOKENS = 8192

REQUIRED_FIELDS = (
    "correlation_id",
    "target_app",
    "git_branch",
    "work_item_description",
    "agent_config",
    "tools",
    "github_token",
    "callback_url",
)
REQUIRED_AGENT_CONFIG_FIELDS = ("system_prompt", "model", "api_key")
REQUIRED_TOOL_FIELDS = ("name", "description", "input_schema")

# Every tool name this process actually knows how to execute. The tool SET
# itself is no longer fixed here — it arrives per-dispatch in payload["tools"],
# defined as real shapes in the "sandbox_tool_defs" ad-hoc sub-process on the
# Dev Agent BPMN map (see one_bpmn's api/compilation.py::_resolve_sandbox_tool_shapes
# and agent_sandbox_ops.py::dispatch). This set exists purely so a diagram
# that names a tool this process has no implementation for fails the dispatch
# loudly at validation time, rather than the model discovering it mid-loop as
# a silent per-call "unknown tool" error.
_KNOWN_TOOL_NAMES = frozenset({
    "read_file", "write_file", "edit_file", "list_files", "run_tests", "open_pull_request",
})

# In-memory job status, keyed by correlation_id. Fine for one-instance-per-run
# (Cloud Run --concurrency=1) — no shared state needed across instances.
_jobs = {}
_jobs_lock = threading.Lock()


def _set_status(correlation_id, **fields):
    with _jobs_lock:
        job = _jobs.setdefault(correlation_id, {})
        job.update(fields)
        job["updated_at"] = time.time()


_SLOW_ACTIONS = frozenset({"run_tests", "open_pull_request"})
_FAST_ACTIONS = frozenset({"read_file", "write_file", "edit_file", "list_files"})

REQUIRED_ACTION_FIELDS = (
    "correlation_id", "action", "target_app", "git_branch", "work_item_description",
    "github_token", "callback_url",
)


def _validate_callback_url(payload):
    callback_url = payload["callback_url"]
    parsed = urllib.parse.urlparse(callback_url)
    if parsed.scheme != "https":
        return "callback_url must be https"
    if ALLOWED_CALLBACK_HOST and parsed.hostname != ALLOWED_CALLBACK_HOST:
        return f"callback_url host must be {ALLOWED_CALLBACK_HOST}"
    return None


def _validate_payload(payload):
    """POST /run's payload is one of two shapes now:

    - The original bundled dispatch (agent_config + tools) — dispatch_to_sandbox's
      own shape, kept working exactly as before while its fate is undecided.
    - A single slow action (run_tests/open_pull_request), each now its own
      real, directly-callable BPMN tool that dispatches independently
      (fast actions — read_file/write_file/edit_file/list_files — go
      through POST /tool_call instead, synchronously; see
      _validate_tool_call_payload)."""
    if "action" in payload:
        missing = [f for f in REQUIRED_ACTION_FIELDS if f not in payload]
        if missing:
            return f"Missing required field(s): {', '.join(missing)}"
        if payload["action"] not in _SLOW_ACTIONS:
            return (
                f"action {payload['action']!r} is not valid for /run. "
                f"Known slow actions: {', '.join(sorted(_SLOW_ACTIONS))}."
            )
        if not (payload.get("github_token") or "").strip():
            return "github_token must be a non-empty string"
        return _validate_callback_url(payload)

    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"

    agent_config = payload["agent_config"]
    if not isinstance(agent_config, dict):
        return "agent_config must be an object"
    missing_ac = [f for f in REQUIRED_AGENT_CONFIG_FIELDS if not (agent_config.get(f) or "").strip()]
    if missing_ac:
        return f"agent_config missing required non-empty field(s): {', '.join(missing_ac)}"

    tools = payload["tools"]
    if not isinstance(tools, list) or not tools:
        return "tools must be a non-empty list — Dev Agent's sandbox_tool_defs sub-process supplied none"
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            return f"tools[{i}] must be an object"
        missing_tf = [f for f in REQUIRED_TOOL_FIELDS if f not in tool]
        if missing_tf:
            return f"tools[{i}] missing required field(s): {', '.join(missing_tf)}"
        if tool["name"] not in _KNOWN_TOOL_NAMES:
            return (
                f"tools[{i}] names {tool['name']!r}, which this sandbox has no implementation for. "
                f"Known tools: {', '.join(sorted(_KNOWN_TOOL_NAMES))}."
            )

    if not (payload.get("github_token") or "").strip():
        return "github_token must be a non-empty string"

    return _validate_callback_url(payload)


def _validate_tool_call_payload(payload):
    """POST /tool_call's payload — one fast, synchronous action against an
    already-(or newly-)cloned working tree. No callback_url/correlation_id:
    this answers inline, there's nothing to park or call back to."""
    required = ("action", "target_app", "git_branch", "work_item_description", "github_token")
    missing = [f for f in required if not str(payload.get(f) or "").strip()]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"
    if payload["action"] not in _FAST_ACTIONS:
        return (
            f"action {payload['action']!r} is not valid for /tool_call. "
            f"Known fast actions: {', '.join(sorted(_FAST_ACTIONS))}."
        )
    if not isinstance(payload.get("args") or {}, dict):
        return "args must be an object"
    return None


def _run(cmd, **kwargs):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
    except subprocess.TimeoutExpired as exc:
        # A hung command has to read as a failed one: nothing above this
        # catches the exception, so it used to kill the job thread silently.
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        err = f"{err}\ntimed out after {exc.timeout:.0f}s: {cmd}".strip()
        return subprocess.CompletedProcess(cmd, 124, out, err)


def _checkout_target_branch(target_app, git_branch):
    # The bake-time clone stages (02a-02d, via lib_clone_functions.sh) strip
    # every private repo's remote URL back to a credential-free
    # https://github.com/... form right after cloning (see that file's
    # header for why: a token embedded in the URL would
    # otherwise sit in plaintext in a committed image layer forever). That
    # means this runtime fetch has nothing to authenticate with unless it
    # re-embeds a credential itself — mirror the bake-time pattern: embed
    # GITHUB_TOKEN in the URL only for the fetch, then always restore the
    # clean URL afterward, whether the fetch/checkout succeeded or not.
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()

    def _scrub(text):
        # git prints the remote URL (credential embedded) in its own stderr
        # on an auth failure — this text flows into the callback body and
        # gets stored on the AI Agent Run, so the token must never reach it.
        return text.replace(github_token, "***") if github_token else text

    clean_url = _run(f"cd {app_dir} && git remote get-url origin").stdout.strip()
    authed = bool(github_token) and clean_url.startswith("https://github.com/")
    if authed:
        authed_url = clean_url.replace("https://github.com/", f"https://{github_token}@github.com/", 1)
        _run(f"cd {app_dir} && git remote set-url origin {authed_url}")
    try:
        fetch = _run(f"cd {app_dir} && git fetch origin {git_branch}")
        if fetch.returncode != 0:
            return False, f"git fetch failed: {_scrub(fetch.stderr)}"
        checkout = _run(
            f"cd {app_dir} && git checkout {git_branch} || git checkout -b {git_branch} origin/{git_branch}"
        )
        if checkout.returncode != 0:
            return False, f"git checkout failed: {_scrub(checkout.stderr)}"
        return True, None
    finally:
        if authed:
            _run(f"cd {app_dir} && git remote set-url origin {clean_url}")


@contextlib.contextmanager
def _authed_remote(app_dir, github_token):
    """Temporarily embeds github_token in origin's URL for the duration of
    the block, then always restores the credential-free URL — mirrors the
    bake-time clone stages' own remote-scrubbing (lib_clone_functions.sh's
    _normalize_remote; a token left in .git/config would otherwise persist
    on disk between calls)."""
    clean_url = _run(f"cd {app_dir} && git remote get-url origin").stdout.strip()
    authed = bool(github_token) and clean_url.startswith("https://github.com/")
    if authed:
        authed_url = clean_url.replace("https://github.com/", f"https://{github_token}@github.com/", 1)
        _run(f"cd {app_dir} && git remote set-url origin {authed_url}")
    try:
        yield
    finally:
        if authed:
            _run(f"cd {app_dir} && git remote set-url origin {clean_url}")


def _head_branch_for(target_app, git_branch, work_item_description):
    """Deterministic per-work-order branch name — same formula _open_pr has
    always used, so retries of the same work order (same three inputs)
    always converge on the same branch, whether they land on this container
    instance or a fresh one."""
    work_hash = hashlib.sha256(
        f"{target_app}:{git_branch}:{work_item_description}".encode("utf-8")
    ).hexdigest()[:16]
    return f"dev-agent/{work_hash}"


def _checkout_or_create_head_branch(target_app, git_branch, head_branch):
    """Like _checkout_target_branch, but for a branch that may not exist
    remotely yet: every one of the 6 sandbox tools is now its own,
    independently-dispatched call (no shared session), so the FIRST call
    for a work order has to create the working branch, and every call after
    it just needs to find it already there. Pushing it immediately (not
    just committing locally) is what makes that durable across calls that
    might land on a different container instance — the git branch itself
    is the shared state between calls, not this instance's local disk."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()

    def _scrub(text):
        return text.replace(github_token, "***") if github_token else text

    with _authed_remote(app_dir, github_token):
        fetch_head = _run(f"cd {app_dir} && git fetch origin {head_branch}")
        if fetch_head.returncode == 0:
            checkout = _run(
                f"cd {app_dir} && git checkout {head_branch} || git checkout -b {head_branch} origin/{head_branch}"
            )
            if checkout.returncode != 0:
                return False, f"git checkout of {head_branch} failed: {_scrub(checkout.stderr)}"
            return True, None

        fetch_base = _run(f"cd {app_dir} && git fetch origin {git_branch}")
        if fetch_base.returncode != 0:
            return False, f"git fetch of base branch {git_branch} failed: {_scrub(fetch_base.stderr)}"
        checkout_base = _run(
            f"cd {app_dir} && git checkout {git_branch} || git checkout -b {git_branch} origin/{git_branch}"
        )
        if checkout_base.returncode != 0:
            return False, f"git checkout of base branch {git_branch} failed: {_scrub(checkout_base.stderr)}"
        new_branch = _run(f"cd {app_dir} && git checkout -B {head_branch}")
        if new_branch.returncode != 0:
            return False, f"git checkout -B {head_branch} failed: {_scrub(new_branch.stderr)}"
        push = _run(f"cd {app_dir} && git push -u origin {head_branch}")
        if push.returncode != 0:
            return False, f"git push of {head_branch} failed: {_scrub(push.stderr)}"
        return True, None


def _commit_and_push(target_app, head_branch, path, message):
    """Durably records a write_file/edit_file change on the head branch
    immediately — not left as a local, uncommitted change — so the NEXT
    tool call for this work order (run_tests, open_pull_request, or even
    another write_file) sees it regardless of which container instance
    serves that call."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    with _authed_remote(app_dir, github_token):
        _run(f"cd {app_dir} && git add -- {shlex.quote(path)}")
        commit = _run(
            f"cd {app_dir} && git -c user.email=dev-agent@sandbox -c user.name='Dev Agent' "
            f"commit -q -m {shlex.quote(message)}"
        )
        if commit.returncode != 0:
            # Nothing to commit (e.g. a write_file that produced identical
            # content) isn't a failure worth surfacing to the model.
            return True, None
        push = _run(f"cd {app_dir} && git push origin {head_branch}")
        if push.returncode != 0:
            return False, push.stderr[-1000:]
        return True, None


def _migrate_site():
    # Cheap once the site is baked (mostly a no-op) — necessary because the
    # branch just checked out for the target app can carry schema changes
    # the baked snapshot doesn't have yet.
    result = _run(f"cd {BENCH_DIR} && bench --site {SITE_NAME} migrate --skip-failing", timeout=600)
    return result.returncode == 0, result.stdout[-2000:], result.stderr[-2000:]


def _run_tests(target_app):
    # mobile_app_ionic isn't a Frappe app — no bench run-tests target exists
    # for it at all. Scope deliberately kept to build+unit-tests only:
    # yarn build (catches TS/template errors, matches CI) + vitest unit
    # tests. Cypress e2e is out of scope on purpose — it needs a running
    # app + backend to hit, which this sandbox has no business standing up
    # for a single dispatched change.
    if target_app == "mobile_app_ionic":
        app_dir = f"{BENCH_DIR}/apps/{target_app}"
        build = _run(f"cd {app_dir} && yarn build", timeout=900)
        if build.returncode != 0:
            return False, build.stdout[-4000:], build.stderr[-4000:]
        test = _run(f"cd {app_dir} && yarn test:unit", timeout=900)
        stdout = (build.stdout + test.stdout)[-4000:]
        stderr = (build.stderr + test.stderr)[-4000:]
        return test.returncode == 0, stdout, stderr

    # --skip-before-tests: erpnext's before_tests hook creates a default
    # Company on any fresh site, which cascades into Warehouse creation and
    # a genuine, pre-existing one_fm bug (before_insert_warehouse assumes a
    # Custom Field — one_fm_project — that only exists as live, un-exported
    # data, not a fixture) — crashing every test run on a fresh site
    # regardless of what the actual change touches. Confirmed live: a real
    # dispatch whose only change was adding a docstring to one_bpmn/utils.py
    # still failed here. This is a workaround for that unrelated fixture
    # gap, not a fix for it — one_fm's own bug is untouched and would still
    # break a real Company/Warehouse creation anywhere else it happens.
    result = _run(
        f"cd {BENCH_DIR} && bench --site {SITE_NAME} run-tests --app {target_app} --skip-before-tests",
        timeout=1800,
    )
    return result.returncode == 0, result.stdout[-4000:], result.stderr[-4000:]


def _collect_changed_files(target_app):
    """{repo-relative path: final content} for every file the coding loop
    touched, relative to the branch this app was checked out at — the shape
    _open_pr below (and Processa's own audit copy of the run) expects."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    diff = _run(f"cd {app_dir} && git diff --name-only HEAD")
    if diff.returncode != 0 or not diff.stdout.strip():
        return {}

    files = {}
    for rel_path in diff.stdout.strip().splitlines():
        abs_path = f"{app_dir}/{rel_path}"
        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                files[rel_path] = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[dev_agent_server] skipping {rel_path} in changed-files capture: {exc}")
    return files


# ── Opening the pull request ─────────────────────────────────────────────
# This process now delivers the change itself — no callback round-trip to
# Processa in between. Committing via the GitHub Contents API (not a local
# git push) mirrors api/github_sync.py's own open_customization_pr() exactly,
# for the same reason that function gives for itself: no push credentials or
# local git state to manage, just the REST API, and the repo is derived from
# the same remote _checkout_target_branch already cloned from — no separate
# "repo" field needs to travel in the payload at all.

def _github_request(method, url, token, ok=(200, 201), json_body=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()

    if status not in ok:
        raise RuntimeError(f"GitHub API error ({status}) on {method} {url}: {body[:300]!r}")
    if status == 404:
        return None
    return json.loads(body) if body else {}


def _repo_for_local_clone(target_app):
    """"owner/repo" from the remote this app was already cloned from —
    the same repo _checkout_target_branch just fetched and checked out."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    result = _run(f"cd {app_dir} && git remote get-url origin")
    if result.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", result.stdout.strip())
    return match.group(1) if match else None


def _open_pr(
    target_app, git_branch, work_item_description, files, github_token, correlation_id, agent_report,
    tests_passed=True, stderr_tail="",
):
    """Create a branch off git_branch, commit every changed file via the
    Contents API, and open a PR. Returns (pr_url, None) on success or
    (None, error_message) on failure — never raises, so a PR-delivery
    problem is reported back like any other outcome, not a crash.

    Opens regardless of tests_passed — a failing run's own real test output
    is repeatedly environment/fixture noise unrelated to the change itself
    (see the Warehouse fixture history), and discarding a genuinely good
    change over that cost more than the old all-or-nothing gate protected.
    The title/body make the outcome impossible to miss instead: a failing
    PR still reads as failing to whoever reviews it, it just isn't thrown
    away before a person ever sees it."""
    repo = _repo_for_local_clone(target_app)
    if not repo:
        return None, f"Could not determine the GitHub repository for {target_app!r} from its local clone."

    # Derived from the work order's own content, not correlation_id — every
    # dispatch creates a brand-new Agent Sandbox Run with its own unique
    # correlation_id, so keying the branch to it meant a model that calls
    # dispatch_to_sandbox more than once for the same brief (confirmed
    # happening live) left one PR behind per attempt. This converges
    # retries of the same work order onto the same branch, and the PR
    # creation below is made idempotent to match.
    work_hash = hashlib.sha256(
        f"{target_app}:{git_branch}:{work_item_description}".encode("utf-8")
    ).hexdigest()[:16]
    head_branch = f"dev-agent/{work_hash}"
    title_prefix = "" if tests_passed else "⚠️ Tests failed: "
    title = f"{title_prefix}Dev Agent: {work_item_description[:72]}"
    file_list = "\n".join(f"- `{path}`" for path in sorted(files))
    if tests_passed:
        testing_section = (
            "The target app's real test suite passed in an isolated, disposable "
            "sandbox before this PR was opened. Review as you would any other PR."
        )
    else:
        testing_section = (
            "**⚠️ The target app's real test suite FAILED in the sandbox "
            "for this change.** It's opened anyway so the diff isn't lost to a "
            "failure that may be unrelated to it (a fresh-site fixture gap, for "
            "example) — but treat this as unverified, not as a normal passing PR. "
            "Confirm the failure's actual cause before merging.\n\n"
            f"```\n{(stderr_tail or '(no output captured)')[-3000:]}\n```"
        )
    body = (
        f"Opened automatically by the Dev Agent sandbox ({correlation_id}).\n\n"
        f"## Work order\n\n{work_item_description}\n\n"
        f"## What changed\n\n{agent_report.strip() or '(the agent finished without a summary)'}\n\n"
        f"## Files changed\n\n{file_list}\n\n"
        f"## Testing\n\n{testing_section}"
    )

    try:
        ref = _github_request("GET", f"{_GITHUB_API}/repos/{repo}/git/ref/heads/{git_branch}", github_token)
        base_sha = ref["object"]["sha"]
        _github_request(
            "POST",
            f"{_GITHUB_API}/repos/{repo}/git/refs",
            github_token,
            ok=(201, 422),  # 422: branch already exists — fine, reuse it
            json_body={"ref": f"refs/heads/{head_branch}", "sha": base_sha},
        )

        for path, content in files.items():
            existing = _github_request(
                "GET",
                f"{_GITHUB_API}/repos/{repo}/contents/{path}?ref={head_branch}",
                github_token,
                ok=(200, 404),
            )
            commit_body = {
                "message": title,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": head_branch,
            }
            if existing and existing.get("sha"):
                commit_body["sha"] = existing["sha"]
            _github_request("PUT", f"{_GITHUB_API}/repos/{repo}/contents/{path}", github_token, json_body=commit_body)

        pr = _github_request(
            "POST",
            f"{_GITHUB_API}/repos/{repo}/pulls",
            github_token,
            ok=(200, 201, 422),  # 422: a PR for this branch already exists — a retry, not a failure
            json_body={"title": title, "head": head_branch, "base": git_branch, "body": body},
        )
        pr_url, pr_number = pr.get("html_url", ""), pr.get("number")
        if not pr_url:
            # Find the existing one instead of treating this as a failure —
            # the commit above already updated its files to this attempt's.
            owner = repo.split("/")[0]
            existing = _github_request(
                "GET", f"{_GITHUB_API}/repos/{repo}/pulls?head={owner}:{head_branch}&state=open", github_token,
            )
            if existing:
                pr_url, pr_number = existing[0].get("html_url", ""), existing[0].get("number")
        if pr_number:
            # Keep title/body in sync with the latest attempt — e.g. a
            # retry that now passes must not leave the "Tests failed"
            # prefix from an earlier attempt sitting on the PR.
            _github_request(
                "PATCH", f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}", github_token,
                json_body={"title": title, "body": body},
            )
        return (pr_url or None), (None if pr_url else "A pull request for this work already existed but its URL could not be resolved.")
    except Exception as exc:  # noqa: BLE001 — reported to the caller, not raised
        return None, str(exc)[:500]


# ── The coding loop's own tools ──────────────────────────────────────────
# Fixed operational primitives, scoped to one app's working tree. These carry
# no judgment of their own — every decision (what to read, what to write,
# when to stop) is the model's, driven by agent_config's system_prompt.

def _safe_path(app_dir, rel_path):
    """Resolve rel_path under app_dir, or raise — the one thing every tool
    below must not get wrong, since the model's own arguments are the only
    input here and must never be trusted to stay inside the sandbox's scope
    on their own."""
    abs_path = os.path.realpath(os.path.join(app_dir, rel_path or ""))
    if abs_path != app_dir and not abs_path.startswith(app_dir + os.sep):
        raise ValueError(f"path escapes the app directory: {rel_path!r}")
    return abs_path


def _tool_read_file(app_dir, args):
    path = args.get("path") or ""
    try:
        abs_path = _safe_path(app_dir, path)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            return {"found": True, "content": fh.read()}
    except FileNotFoundError:
        return {"found": False, "content": ""}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": str(exc)}


def _tool_write_file(app_dir, args):
    path = args.get("path") or ""
    content = args.get("content")
    if content is None:
        return {"error": "content is required"}
    try:
        abs_path = _safe_path(app_dir, path)
    except ValueError as exc:
        return {"error": str(exc)}
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return {"written": True, "path": path}


def _tool_edit_file(app_dir, args):
    """Targeted search/replace, complementing write_file's full overwrite —
    mirrors the familiar old_string/new_string Edit-tool convention. Requires
    old_string to match EXACTLY once: zero matches means the model's premise
    about the file's current content is wrong (stale read), and more than one
    means the replacement location is ambiguous — both are reported back as
    errors rather than guessed at, same reasoning as any editor tool that
    works this way."""
    path = args.get("path") or ""
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    if old_string is None or new_string is None:
        return {"error": "old_string and new_string are both required"}
    try:
        abs_path = _safe_path(app_dir, path)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        return {"error": f"{path!r} does not exist — use write_file to create it"}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": str(exc)}
    count = content.count(old_string)
    if count == 0:
        return {"error": "old_string not found in the file — it may have changed since you last read it"}
    if count > 1:
        return {"error": f"old_string appears {count} times — include more surrounding context to make it unique"}
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content.replace(old_string, new_string, 1))
    return {"edited": True, "path": path}


_LIST_FILES_MAX = 500


def _tool_list_files(app_dir, args):
    prefix = (args.get("path_prefix") or "").strip().lstrip("/")
    try:
        start_dir = _safe_path(app_dir, prefix) if prefix else app_dir
    except ValueError as exc:
        return {"error": str(exc)}
    paths = []
    for root, dirs, filenames in os.walk(start_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in filenames:
            rel = os.path.relpath(os.path.join(root, name), app_dir)
            paths.append(rel)
            if len(paths) >= _LIST_FILES_MAX:
                return {"files": paths, "count": len(paths), "truncated": True}
    return {"files": paths, "count": len(paths), "truncated": False}


def _tool_run_tests(target_app, args):
    passed, stdout, stderr = _run_tests(target_app)
    return {"passed": passed, "stdout_tail": stdout[-2000:], "stderr_tail": stderr[-2000:]}


def _tool_open_pull_request(target_app, args, run_ctx):
    """PR-creation as something the model explicitly decides to call, rather
    than automatic post-loop logic (run_job no longer calls _open_pr itself —
    see its own docstring). Re-runs the real test suite HERE, independent of
    whatever the model last saw from its own run_tests calls, so the PR's
    pass/fail flagging is always accurate to the working tree's current state
    at the moment of opening — not dependent on the model remembering to
    re-test after its last edit before calling this."""
    summary = (args.get("summary") or "").strip()
    files = _collect_changed_files(target_app)
    if not files:
        return {"error": "no changes to commit yet — nothing to open a pull request for"}
    passed, _stdout, stderr = _run_tests(target_app)
    pr_url, pr_error = _open_pr(
        target_app, run_ctx["git_branch"], run_ctx["work_item_description"], files,
        run_ctx["github_token"], run_ctx["correlation_id"], summary,
        tests_passed=passed, stderr_tail=stderr,
    )
    if pr_url:
        return {"pr_url": pr_url, "tests_passed": passed}
    return {"error": pr_error, "tests_passed": passed}


def _dispatch_tool(app_dir, target_app, name, args, run_ctx):
    if name == "read_file":
        return _tool_read_file(app_dir, args)
    if name == "write_file":
        return _tool_write_file(app_dir, args)
    if name == "edit_file":
        return _tool_edit_file(app_dir, args)
    if name == "list_files":
        return _tool_list_files(app_dir, args)
    if name == "run_tests":
        return _tool_run_tests(target_app, args)
    if name == "open_pull_request":
        return _tool_open_pull_request(target_app, args, run_ctx)
    # _validate_payload already rejects a dispatch naming an unimplemented
    # tool before the loop ever starts (see _KNOWN_TOOL_NAMES) — reaching
    # this means _KNOWN_TOOL_NAMES and this dispatcher drifted apart, not a
    # normal runtime outcome. Reported to the model like any other tool
    # error rather than crashing the loop.
    return {"error": f"unknown tool {name!r} — the sandbox has no implementation for it"}


def _handle_tool_call(payload):
    """POST /tool_call's synchronous handler — one fast action
    (read_file/write_file/edit_file/list_files) against the work order's
    own deterministic head branch, answered inline. Each call independently
    ensures the branch exists (creating it off git_branch on the very first
    call for a work order) since there's no shared session between calls —
    the git branch itself is what carries state from one call to the next,
    not this container instance's local disk."""
    action = payload["action"]
    target_app = payload["target_app"]
    git_branch = payload["git_branch"]
    work_item_description = payload["work_item_description"]
    args = payload.get("args") or {}

    head_branch = _head_branch_for(target_app, git_branch, work_item_description)
    ok, err = _checkout_or_create_head_branch(target_app, git_branch, head_branch)
    if not ok:
        return {"error": err}

    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    if action == "read_file":
        return _tool_read_file(app_dir, args)
    if action == "list_files":
        return _tool_list_files(app_dir, args)
    if action == "write_file":
        result = _tool_write_file(app_dir, args)
        if result.get("written"):
            ok, err = _commit_and_push(target_app, head_branch, args.get("path") or "", "write_file via Dev Agent")
            if not ok:
                result["commit_error"] = err
        return result
    if action == "edit_file":
        result = _tool_edit_file(app_dir, args)
        if result.get("edited"):
            ok, err = _commit_and_push(target_app, head_branch, args.get("path") or "", "edit_file via Dev Agent")
            if not ok:
                result["commit_error"] = err
        return result
    # _validate_tool_call_payload already restricts action to _FAST_ACTIONS —
    # reaching this means that set and this dispatcher drifted apart.
    return {"error": f"unknown fast tool action {action!r}"}


def _report_unexpected_failure(job, payload):
    """Run a background job so an exception it lets escape still reaches
    Processa; a daemon thread that dies silently leaves /status frozen and
    the caller waiting out its whole deadline."""
    try:
        job(payload)
    except Exception as exc:  # noqa: BLE001 — the last place that can still report it
        correlation_id = payload.get("correlation_id")
        error = f"{type(exc).__name__}: {str(exc)[:400]}"
        _set_status(correlation_id, state="failed", error=error)
        _post_callback(payload.get("callback_url"), {"correlation_id": correlation_id, "status": "failed", "error": error})


def run_single_action_job(payload):
    """Background job for POST /run's action-based shape (run_tests /
    open_pull_request) — the two sandbox tools slow enough to need the same
    accept-then-callback shape run_job already uses, just for one action
    instead of a whole bundled coding session."""
    correlation_id = payload["correlation_id"]
    action = payload["action"]
    target_app = payload["target_app"]
    git_branch = payload["git_branch"]
    work_item_description = payload["work_item_description"]
    github_token = payload["github_token"]
    args = payload.get("args") or {}
    callback_url = payload["callback_url"]

    head_branch = _head_branch_for(target_app, git_branch, work_item_description)

    _set_status(correlation_id, state="checking_out_branch")
    ok, err = _checkout_or_create_head_branch(target_app, git_branch, head_branch)
    if not ok:
        _set_status(correlation_id, state="failed", error=err)
        _post_callback(callback_url, {"correlation_id": correlation_id, "status": "failed", "error": err})
        return

    _set_status(correlation_id, state="migrating")
    ok, mig_out, mig_err = _migrate_site()
    if not ok:
        _set_status(correlation_id, state="failed", error=mig_err)
        _post_callback(
            callback_url,
            {"correlation_id": correlation_id, "status": "failed", "error": mig_err, "stdout_tail": mig_out},
        )
        return

    if action == "run_tests":
        _set_status(correlation_id, state="running_tests")
        passed, stdout, stderr = _run_tests(target_app)
        body = {
            "correlation_id": correlation_id,
            "status": "tests_passed" if passed else "tests_failed",
            "stdout_tail": stdout,
            "stderr_tail": stderr,
        }
    elif action == "open_pull_request":
        run_ctx = {
            "git_branch": git_branch,
            "work_item_description": work_item_description,
            "github_token": github_token,
            "correlation_id": correlation_id,
        }
        _set_status(correlation_id, state="opening_pr")
        result = _tool_open_pull_request(target_app, args, run_ctx)
        status = "tests_passed" if result.get("pr_url") else "tests_failed"
        body = {"correlation_id": correlation_id, "status": status}
        if result.get("pr_url"):
            body["pr_url"] = result["pr_url"]
        if result.get("error"):
            body["pr_error"] = result["error"]
    else:
        # _validate_payload already restricts action to _SLOW_ACTIONS for
        # /run — reaching this means that set and this branch drifted apart.
        body = {"correlation_id": correlation_id, "status": "failed", "error": f"unknown action {action!r}"}

    _set_status(correlation_id, state="done", result=body.get("status"))
    _post_callback(callback_url, body)


def _turn_usage_fields(usage):
    return {
        "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def _run_coding_loop(target_app, work_item_description, agent_config, tools, run_ctx):
    """The agent's own bounded tool-calling loop. Returns a dict: final_text,
    iterations_used, hit_limit, usage (accumulated token counts across every
    turn — Processa has no other way to see what this loop actually cost,
    since it runs entirely outside Frappe), trace (one entry per turn —
    {role, content, tool_calls: [{name, arguments, result, status}], token/
    latency fields — matching the shape one_bpmn's own step-driven loop
    already produces, so Processa can turn each turn into a real AI Agent
    Step via record_ai_step() without any new shape to reconcile), started_at/
    ended_at (unix seconds).

    ``tools`` (Anthropic-format schemas) and the tool SET they represent are
    no longer fixed here — they arrive fresh from the BPMN map on every
    dispatch (see agent_sandbox_ops.py::_sandbox_tools). This function's job
    is unchanged either way: wire whatever tool calls the model makes to the
    filesystem/GitHub and feed results back, exactly as a human would relay
    a tool's output. ``run_ctx`` (git_branch, work_item_description,
    github_token, correlation_id) is what open_pull_request needs that no
    other tool does — bundled here rather than widening every tool's own
    signature for the one that's different."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    client = anthropic.Anthropic(api_key=agent_config["api_key"])
    model = agent_config["model"]
    system_prompt = agent_config["system_prompt"]

    messages = [{"role": "user", "content": work_item_description}]

    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    trace = []
    started_at = time.time()

    def _accumulate(u):
        # Every turn bills separately — a 30-iteration loop is 30 separate
        # API calls, not one, so this has to sum across all of them rather
        # than take the last response's usage as the total.
        for field in usage:
            usage[field] += getattr(u, field, 0) or 0

    for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
        turn_started = time.time()
        response = client.messages.create(
            model=model,
            system=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=AGENT_MAX_TOKENS,
        )
        latency_ms = int((time.time() - turn_started) * 1000)
        _accumulate(response.usage)
        text = "".join(block.text for block in response.content if block.type == "text")

        if response.stop_reason != "tool_use":
            trace.append({
                "role": "assistant", "content": text, "tool_calls": [],
                "latency_ms": latency_ms, **_turn_usage_fields(response.usage),
            })
            return {
                "final_text": text,
                "iterations": iteration,
                "hit_limit": False,
                "usage": usage,
                "trace": trace,
                "started_at": started_at,
                "ended_at": time.time(),
            }

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        turn_calls = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            args = block.input or {}
            result = _dispatch_tool(app_dir, target_app, block.name, args, run_ctx)
            status = "Error" if isinstance(result, dict) and result.get("error") else "Success"
            turn_calls.append({"name": block.name, "arguments": args, "result": result, "status": status})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )
        trace.append({
            "role": "assistant", "content": text, "tool_calls": turn_calls,
            "latency_ms": latency_ms, **_turn_usage_fields(response.usage),
        })
        messages.append({"role": "user", "content": tool_results})

    return {
        "final_text": (
            f"Stopped after {MAX_AGENT_ITERATIONS} tool-calling turns without finishing — "
            "reporting whatever state the working tree is currently in."
        ),
        "iterations": MAX_AGENT_ITERATIONS,
        "hit_limit": True,
        "usage": usage,
        "trace": trace,
        "started_at": started_at,
        "ended_at": time.time(),
    }


def _sign(body_bytes):
    if not CALLBACK_HMAC_SECRET:
        return None
    return hmac.new(CALLBACK_HMAC_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _post_callback(callback_url, body):
    body = {**body, "sandbox_env": SANDBOX_ENV}
    data = json.dumps(body).encode("utf-8")
    signature = _sign(data)
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["X-Signature"] = signature
    else:
        # Only acceptable in local/dev use — production and beta must always
        # have CALLBACK_HMAC_SECRET set via deploy.py's Secret Manager wiring.
        print("[dev_agent_server] WARNING: CALLBACK_HMAC_SECRET unset — sending an UNSIGNED callback.")

    last_error = None
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(callback_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except Exception as exc:  # noqa: BLE001 — retried below; only logged once exhausted
            last_error = exc
            if attempt < CALLBACK_MAX_ATTEMPTS:
                print(f"[dev_agent_server] callback attempt {attempt} to {callback_url} failed: {exc} — retrying")
                time.sleep(CALLBACK_RETRY_BACKOFF_SECONDS * attempt)

    print(f"[dev_agent_server] callback to {callback_url} failed after {CALLBACK_MAX_ATTEMPTS} attempts: {last_error}")
    return None


def _last_open_pr_result(trace):
    """The result dict of the LAST open_pull_request call in the trace, or
    None if the model never called it. run_job uses this to report what
    happened instead of auto-opening a PR itself — open_pull_request is now
    the model's own tool call, not automatic post-loop logic (see its own
    docstring)."""
    result = None
    for turn in trace:
        for call in turn["tool_calls"]:
            if call["name"] == "open_pull_request":
                result = call["result"]
    return result


def run_job(payload):
    correlation_id = payload["correlation_id"]
    target_app = payload["target_app"]
    git_branch = payload["git_branch"]
    work_item_description = payload["work_item_description"]
    agent_config = payload["agent_config"]
    tools = payload["tools"]
    github_token = payload["github_token"]
    callback_url = payload["callback_url"]

    _set_status(correlation_id, state="checking_out_branch")
    ok, err = _checkout_target_branch(target_app, git_branch)
    if not ok:
        _set_status(correlation_id, state="failed", error=err)
        _post_callback(callback_url, {"correlation_id": correlation_id, "status": "failed", "error": err})
        return

    _set_status(correlation_id, state="migrating")
    ok, mig_out, mig_err = _migrate_site()
    if not ok:
        _set_status(correlation_id, state="failed", error=mig_err)
        _post_callback(
            callback_url,
            {"correlation_id": correlation_id, "status": "failed", "error": mig_err, "stdout_tail": mig_out},
        )
        return

    _set_status(correlation_id, state="coding")
    run_ctx = {
        "git_branch": git_branch,
        "work_item_description": work_item_description,
        "github_token": github_token,
        "correlation_id": correlation_id,
    }
    try:
        loop_result = _run_coding_loop(target_app, work_item_description, agent_config, tools, run_ctx)
        agent_report = loop_result["final_text"]
        iterations = loop_result["iterations"]
        hit_limit = loop_result["hit_limit"]
    except Exception as exc:  # noqa: BLE001 — reported to the caller, not raised here
        err = f"coding loop failed: {exc}"
        _set_status(correlation_id, state="failed", error=err)
        _post_callback(callback_url, {"correlation_id": correlation_id, "status": "failed", "error": err})
        return

    # Authoritative, independent of whatever the model's own last run_tests
    # or open_pull_request call happened to see — the working tree may have
    # changed since either of those, and status/callback must reflect the
    # tree as it actually stands right now, not a possibly-stale self-report.
    _set_status(correlation_id, state="running_tests")
    passed, stdout, stderr = _run_tests(target_app)

    status = "tests_passed" if passed else "tests_failed"
    body = {
        "correlation_id": correlation_id,
        "status": status,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "agent_report": agent_report,
        "agent_iterations": iterations,
        "agent_hit_iteration_limit": hit_limit,
        "agent_model": agent_config["model"],
        "agent_usage": loop_result["usage"],
        "agent_trace": loop_result["trace"],
        "agent_started_at": loop_result["started_at"],
        "agent_ended_at": loop_result["ended_at"],
    }
    files = _collect_changed_files(target_app)
    if not files:
        if passed:
            status = body["status"] = "tests_passed_no_changes"
        # else: status stays "tests_failed" — no files means nothing to
        # open a PR for regardless of the test outcome.
    else:
        body["files"] = files
        pr_result = _last_open_pr_result(loop_result["trace"])
        if pr_result and pr_result.get("pr_url"):
            body["pr_url"] = pr_result["pr_url"]
        elif pr_result and pr_result.get("error"):
            body["pr_error"] = pr_result["error"]
        else:
            # The model made real changes but never called open_pull_request
            # at all — a real prompt-reliability gap this design accepts
            # (see agent_sandbox_ops.py's module docstring): flagged clearly
            # rather than silently leaving the diff undelivered with no trace
            # of why.
            body["pr_error"] = "The agent made changes but never called open_pull_request."

    _set_status(correlation_id, state="done", result=status)
    _post_callback(callback_url, body)


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if self.path.startswith("/status/"):
            correlation_id = self.path.rsplit("/", 1)[-1]
            with _jobs_lock:
                job = _jobs.get(correlation_id)
            if job is None:
                self._send_json(404, {"error": "unknown correlation_id"})
            else:
                self._send_json(200, job)
            return
        self._send_json(404, {"error": "not found"})

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw or b"{}")

    def do_POST(self):
        if self.path == "/tool_call":
            self._handle_tool_call_request()
            return
        if self.path != "/run":
            self._send_json(404, {"error": "not found"})
            return

        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        error = _validate_payload(payload)
        if error:
            self._send_json(422, {"error": error})
            return

        correlation_id = payload.get("correlation_id") or str(uuid.uuid4())
        _set_status(correlation_id, state="accepted")
        # action present -> one of the 6 tools' own slow (run_tests/
        # open_pull_request) dispatch; absent -> dispatch_to_sandbox's
        # original bundled coding session.
        target = run_single_action_job if "action" in payload else run_job
        threading.Thread(target=_report_unexpected_failure, args=(target, payload), daemon=True).start()
        self._send_json(202, {"correlation_id": correlation_id, "status": "accepted"})

    def _handle_tool_call_request(self):
        # Synchronous by design — read_file/write_file/edit_file/list_files
        # are seconds-scale (a git fetch against an already-locally-cloned
        # repo, plus a local file op), so there's nothing worth an
        # accept-then-callback round trip over.
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        error = _validate_tool_call_payload(payload)
        if error:
            self._send_json(422, {"error": error})
            return

        try:
            result = _handle_tool_call(payload)
        except Exception as exc:  # noqa: BLE001 — reported to the caller, not a 500
            self._send_json(200, {"error": f"tool_call failed: {exc}"})
            return
        self._send_json(200, result)

    def log_message(self, fmt, *args):
        print(f"[dev_agent_server] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))

    if SANDBOX_ENV != "local" and not CALLBACK_HMAC_SECRET:
        print(
            f"[dev_agent_server] FATAL: SANDBOX_ENV={SANDBOX_ENV} but CALLBACK_HMAC_SECRET is unset. "
            "Refusing to start — every callback from this environment would be unsigned. "
            "Check deploy.py's --set-secrets wiring."
        )
        raise SystemExit(1)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"[dev_agent_server] listening on :{port} — env={SANDBOX_ENV}, "
        f"callback_signing={'on' if CALLBACK_HMAC_SECRET else 'off'}, "
        f"allowed_callback_host={ALLOWED_CALLBACK_HOST or '(any https)'}"
    )
    server.serve_forever()
