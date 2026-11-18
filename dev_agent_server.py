"""dev_agent_server.py

Generic, config-free driver for the dev-agent sandbox container.

This process IS the coding agent. It carries no system prompt, no model
choice, no tool list, and no credential of its own — every one of those
arrives fresh in the POST /run body's "agent_config" (system_prompt, model,
api_key) and "tools" (Anthropic-format schemas), resolved by Processa on
every dispatch: agent_config from the "Dev Agent" AI Agent Configuration and
its linked AI Model, tools from the shapes of the BPMN map's own
"sandbox_tool_defs" ad-hoc sub-process (see processa's
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
import difflib
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
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

BENCH_DIR = os.environ.get("BENCH_DIR", "/home/frappe/frappe-bench")
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
# Dev Agent BPMN map (see processa's api/compilation.py::_resolve_sandbox_tool_shapes
# and agent_sandbox_ops.py::dispatch). This set exists purely so a diagram
# that names a tool this process has no implementation for fails the dispatch
# loudly at validation time, rather than the model discovering it mid-loop as
# a silent per-call "unknown tool" error.
_KNOWN_TOOL_NAMES = frozenset({
    "read_file", "write_file", "edit_file", "delete_file", "list_files", "run_tests", "open_pull_request",
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
_FAST_ACTIONS = frozenset({
    "read_file", "write_file", "edit_file", "delete_file", "list_files", "read_files", "grep_files", "apply_edits",
})

REQUIRED_ACTION_FIELDS = (
    "correlation_id", "action", "target_app", "git_branch", "work_item_description",
    "github_token", "callback_url",
)


_SAFE_APP = re.compile(r"\A[A-Za-z0-9_]{1,64}\Z")
_SAFE_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,180}\Z")


def _validate_identifiers(payload):
    """target_app and git_branch are interpolated into shell commands, so they
    are checked against what a real app directory and git ref may contain
    before any of them runs. Rejected here rather than escaped later: a value
    outside these sets is a caller bug, not something to sanitise and obey."""
    app = str(payload.get("target_app") or "").strip()
    if not _SAFE_APP.match(app):
        return f"target_app {app!r} is not a valid app name (letters, digits and underscore only)."
    ref = str(payload.get("git_branch") or "").strip()
    if not _SAFE_REF.match(ref) or ".." in ref or ref.endswith((".lock", "/", ".")):
        return f"git_branch {ref!r} is not a valid branch name."
    return None


def _allowed_callback_hosts():
    """The hostnames the sandbox may call back to: ALLOWED_CALLBACK_HOST holds one, or several separated by commas."""
    return {host.strip().lower() for host in ALLOWED_CALLBACK_HOST.split(",") if host.strip()}


def _validate_callback_url(payload):
    callback_url = payload["callback_url"]
    parsed = urllib.parse.urlparse(callback_url)
    if parsed.scheme != "https":
        return "callback_url must be https"
    allowed = _allowed_callback_hosts()
    if allowed and parsed.hostname not in allowed:
        return f"callback_url host must be one of: {', '.join(sorted(allowed))}"
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
        return _validate_identifiers(payload) or _validate_callback_url(payload)

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

    return _validate_identifiers(payload) or _validate_callback_url(payload)


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
    return _validate_identifiers(payload)


class _Timings:
    """Milliseconds one request spends in each kind of work (git, tool, migrate, tests, pr)."""

    def __init__(self):
        self.ms = {}
        self._started = time.monotonic()

    @contextlib.contextmanager
    def span(self, name):
        began = time.monotonic()
        try:
            yield
        finally:
            self.ms[name] = self.ms.get(name, 0) + int((time.monotonic() - began) * 1000)

    def report(self):
        return {**self.ms, "total": int((time.monotonic() - self._started) * 1000)}


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

    clean_url = _run(f"cd {shlex.quote(app_dir)} && git remote get-url origin").stdout.strip()
    authed = bool(github_token) and clean_url.startswith("https://github.com/")
    if authed:
        authed_url = clean_url.replace("https://github.com/", f"https://{github_token}@github.com/", 1)
        _run(f"cd {shlex.quote(app_dir)} && git remote set-url origin {shlex.quote(authed_url)}")
    try:
        fetch = _run(f"cd {shlex.quote(app_dir)} && git fetch origin {shlex.quote(git_branch)}")
        if fetch.returncode != 0:
            return False, f"git fetch failed: {_scrub(fetch.stderr)}"
        # -f: the app dirs are cloned once at bake time, not fresh per run, so
        # a tracked file can already differ from FETCH_HEAD before this call
        # ever runs (e.g. a line-ending mismatch baked into the image) — a
        # plain checkout refuses instead of switching. Nothing on this disk
        # between calls is meant to survive anyway (see the comment above
        # _checkout_or_create_head_branch), so discarding it here is safe.
        checkout = _run(f"cd {shlex.quote(app_dir)} && git checkout -f -B {shlex.quote(git_branch)} FETCH_HEAD")
        if checkout.returncode != 0:
            return False, f"git checkout failed: {_scrub(checkout.stderr)}"
        return True, None
    finally:
        if authed:
            _run(f"cd {shlex.quote(app_dir)} && git remote set-url origin {shlex.quote(clean_url)}")


@contextlib.contextmanager
def _authed_remote(app_dir, github_token):
    """Temporarily embeds github_token in origin's URL for the duration of
    the block, then always restores the credential-free URL — mirrors the
    bake-time clone stages' own remote-scrubbing (lib_clone_functions.sh's
    _normalize_remote; a token left in .git/config would otherwise persist
    on disk between calls)."""
    clean_url = _run(f"cd {shlex.quote(app_dir)} && git remote get-url origin").stdout.strip()
    authed = bool(github_token) and clean_url.startswith("https://github.com/")
    if authed:
        authed_url = clean_url.replace("https://github.com/", f"https://{github_token}@github.com/", 1)
        _run(f"cd {shlex.quote(app_dir)} && git remote set-url origin {shlex.quote(authed_url)}")
    try:
        yield
    finally:
        if authed:
            _run(f"cd {shlex.quote(app_dir)} && git remote set-url origin {shlex.quote(clean_url)}")


_BRANCH_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]+")


def _branch_from_work_item_id(work_item_id):
    """The Work Item id as a git ref name, or "" when nothing usable is left.
    Sanitised, never invented — the hashed name is the fallback, not a guess."""
    name = _BRANCH_UNSAFE.sub("-", (work_item_id or "").strip()).strip("-/.")
    while ".." in name:
        name = name.replace("..", ".")
    return name[:100]


def _head_branch_for(target_app, git_branch, work_item_description, work_item_id=None):
    """The one branch every call for a work order lands on: the Work Item id
    when the caller has one, else a hash of the three inputs so retries converge."""
    named = _branch_from_work_item_id(work_item_id)
    if named:
        return named
    work_hash = hashlib.sha256(
        f"{target_app}:{git_branch}:{work_item_description}".encode("utf-8")
    ).hexdigest()[:16]
    return f"dev-agent/{work_hash}"


# Both checkouts take FETCH_HEAD rather than a local or remote-tracking branch.
# The local base branch is whatever the image was baked with — every run built on
# it was 44 commits behind — and on a single-branch clone origin/<branch> never
# exists for anything but the clone's own branch, which broke retries of a Work
# Item on a fresh instance. What was just fetched is the one ref that is always
# present and always current.
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
        # The base is fetched into its own tracking ref on every call: a clone that kept only its default
        # branch has no origin/<base>, and the diff for the pull request is taken against it.
        base_refspec = f"+refs/heads/{git_branch}:refs/remotes/origin/{git_branch}"
        fetch_base_ref = _run(f"cd {shlex.quote(app_dir)} && git fetch origin {shlex.quote(base_refspec)}")
        if fetch_base_ref.returncode != 0:
            return False, f"git fetch of base branch {git_branch} failed: {_scrub(fetch_base_ref.stderr)}"
        fetch_head = _run(f"cd {shlex.quote(app_dir)} && git fetch origin {shlex.quote(head_branch)}")
        if fetch_head.returncode == 0:
            # -f: see _checkout_target_branch — the working tree can already
            # be dirty from bake time, and nothing in it is worth keeping.
            checkout = _run(
                f"cd {shlex.quote(app_dir)} && git checkout -f -B {shlex.quote(head_branch)} FETCH_HEAD && git clean -fdq"
            )
            if checkout.returncode != 0:
                return False, f"git checkout of {head_branch} failed: {_scrub(checkout.stderr)}"
            return True, None

        fetch_base = _run(f"cd {shlex.quote(app_dir)} && git fetch origin {shlex.quote(git_branch)}")
        if fetch_base.returncode != 0:
            return False, f"git fetch of base branch {git_branch} failed: {_scrub(fetch_base.stderr)}"
        # -f: see _checkout_target_branch.
        checkout_base = _run(
            f"cd {shlex.quote(app_dir)} && git checkout -f -B {shlex.quote(git_branch)} FETCH_HEAD && git clean -fdq"
        )
        if checkout_base.returncode != 0:
            return False, f"git checkout of base branch {git_branch} failed: {_scrub(checkout_base.stderr)}"
        new_branch = _run(f"cd {shlex.quote(app_dir)} && git checkout -f -B {shlex.quote(head_branch)}")
        if new_branch.returncode != 0:
            return False, f"git checkout -B {head_branch} failed: {_scrub(new_branch.stderr)}"
        push = _run(f"cd {shlex.quote(app_dir)} && git push -u origin {shlex.quote(head_branch)}")
        if push.returncode != 0:
            return False, f"git push of {head_branch} failed: {_scrub(push.stderr)}"
        return True, None


def _git_identity_for(agent_name):
    """(user.name, user.email) for a commit author, derived from the calling
    Processa agent's own name instead of a fixed 'Dev Agent' literal — every
    sandbox agent (Dev Agent, Bug Agent, any future one) shares this same
    disposable sandbox, so the identity has to come from the request, not
    from a constant baked in when this file only served Dev Agent."""
    name = (agent_name or "Dev Agent").strip() or "Dev Agent"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "dev-agent"
    return name, f"{slug}@sandbox"


def _commit_and_push(target_app, head_branch, path, message, agent_name="Dev Agent"):
    # path is one repo-relative path, or a list of them for a multi-file change.
    """Durably records a write_file/edit_file change on the head branch
    immediately — not left as a local, uncommitted change — so the NEXT
    tool call for this work order (run_tests, open_pull_request, or even
    another write_file) sees it regardless of which container instance
    serves that call."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    user_name, user_email = _git_identity_for(agent_name)
    with _authed_remote(app_dir, github_token):
        for staged in [path] if isinstance(path, str) else path:
            _run(f"cd {shlex.quote(app_dir)} && git add -- {shlex.quote(staged)}")
        commit = _run(
            f"cd {app_dir} && git -c user.email={shlex.quote(user_email)} -c user.name={shlex.quote(user_name)} "
            f"commit -q -m {shlex.quote(message)}"
        )
        if commit.returncode != 0:
            # Nothing to commit (e.g. a write_file that produced identical
            # content) isn't a failure worth surfacing to the model.
            return True, None
        push = _run(f"cd {shlex.quote(app_dir)} && git push origin {shlex.quote(head_branch)}")
        if push.returncode != 0:
            return False, push.stderr[-1000:]
        return True, None


def _changed_paths(target_app, base_branch=None):
    """Repo-relative paths changed on the working branch: committed there (against base_branch) or still pending."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    paths = []
    if base_branch:
        base_ref = f"origin/{base_branch}"
        committed = _run(f"cd {shlex.quote(app_dir)} && git diff --name-only {shlex.quote(base_ref)}...HEAD")
        if committed.returncode == 0:
            paths += committed.stdout.strip().splitlines()
    pending = _run(f"cd {shlex.quote(app_dir)} && git diff --name-only HEAD")
    if pending.returncode == 0:
        paths += pending.stdout.strip().splitlines()
    return list(dict.fromkeys(p for p in paths if p))


_SCHEMA_PATH = re.compile(r"(/doctype/[^/]+/[^/]+\.json$|/patches/|/fixtures/|(^|/)patches\.txt$|/custom/[^/]+\.json$)")


def _needs_migrate(paths):
    """Whether the change can alter the schema, so the site must be migrated before tests."""
    return any(_SCHEMA_PATH.search(p) for p in paths)


def _pending_paths(target_app):
    """Paths with changes not yet committed: modified, deleted or new. A change already pushed is not among them."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    changed = _run(f"cd {shlex.quote(app_dir)} && git diff --name-only HEAD")
    new = _run(f"cd {shlex.quote(app_dir)} && git ls-files --others --exclude-standard")
    return {p for result in (changed, new) if result.returncode == 0 for p in result.stdout.splitlines() if p}


def _migrate_site():
    # Cheap once the site is baked (mostly a no-op) — necessary because the
    # branch just checked out for the target app can carry schema changes
    # the baked snapshot doesn't have yet.
    result = _run(f"cd {shlex.quote(BENCH_DIR)} && bench --site {shlex.quote(SITE_NAME)} migrate --skip-failing", timeout=600)
    return result.returncode == 0, result.stdout[-2000:], result.stderr[-2000:]


# --- Testing Agent: site preparation and PR-head startup ---------------------------------------
# Two slow session commands (see session_agent.py). Everything below runs subprocesses as argument
# lists, never shell strings, and only ever fetches and checks out: no commit, push or merge.
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "/home/frappe/snapshots")
SERVE_PORT = 8000
READY_TIMEOUT_SECONDS = 180
_RUN_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ROLE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _-]{0,59}\Z")
_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
_PASSWORD_MAX = 128
# Where a change needs a Desk bundle rebuilt: the app's public/ assets, plus its own frontend folder.
_FRONTEND_DIRS = {"processa": ("frontend/",)}


def _exec(args, env=None, cwd=None, timeout=None, stdin=None):
    """Run an argument list (no shell). A hung or missing command reads as a failed one, like _run."""
    try:
        extra = {"stdin": stdin} if stdin is not None else {}
        return subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout, **extra)
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(args, 124, "", f"{err}\ntimed out after {exc.timeout:.0f}s".strip())
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", f"{type(exc).__name__}: {exc}")


def _spawn(args, **kwargs):
    return subprocess.Popen(args, **kwargs)


def _tail(result, *secrets):
    text = f"{result.stdout or ''}\n{result.stderr or ''}".strip()[-2000:]
    for secret in filter(None, secrets):
        text = text.replace(secret, "***")
    return text


def _check_run_id(run_id):
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        return f"run_id {run_id!r} is not valid (letters, digits, - and _, at most 64)."


def _check_branch(name, label="base_branch"):
    if not isinstance(name, str) or not _SAFE_REF.match(name) or ".." in name or name.endswith((".lock", "/", ".")):
        return f"{label} {name!r} is not a valid branch name."


def _check_pr_number(number):
    if isinstance(number, bool) or not isinstance(number, (int, str)) or not re.fullmatch(r"[1-9][0-9]{0,8}", str(number)):
        return f"pr_number {number!r} is not a valid pull request number."


def _check_app(target_app):
    if not _SAFE_APP.match(str(target_app or "")):
        return f"target_app {target_app!r} is not a valid app name."


def _snapshot_paths(run_id):
    """(sql path, marker path) for a run; the run id is validated, and the result is checked to stay inside the snapshot dir."""
    root = os.path.realpath(SNAPSHOT_DIR)
    sql = os.path.realpath(os.path.join(root, f"{run_id}.sql"))
    if os.path.dirname(sql) != root:
        raise ValueError("snapshot path escapes the snapshot directory")
    return sql, sql[:-4] + ".json"


def _failed(step, error, **extra):
    return {"status": "failed", "step": step, "error": error, **extra}


def _site_db_credentials():
    sites = os.path.join(BENCH_DIR, "sites")
    conf = {}
    for path in (os.path.join(sites, "common_site_config.json"), os.path.join(sites, SITE_NAME, "site_config.json")):
        with open(path) as fh:
            conf.update(json.load(fh))
    if not conf.get("db_name") or not conf.get("db_password"):
        raise RuntimeError("site_config has no db_name/db_password")
    return conf["db_name"], conf["db_password"], str(conf.get("db_host") or "127.0.0.1"), str(conf.get("db_port") or 3306)


# Runs inside the bench virtualenv (it needs frappe). The password and users arrive in the environment,
# never in argv, so neither shows in a process list or a log. Re-running updates users in place.
_ENSURE_USERS_SCRIPT = """
import json, os, frappe
users = json.loads(os.environ["SANDBOX_TEST_USERS"])
password = os.environ["SANDBOX_TEST_PASSWORD"]
frappe.init(site=os.environ["SANDBOX_SITE"], sites_path=".")
frappe.connect()
try:
    frappe.set_user("Administrator")
    # A fresh site never ran ERPNext's setup wizard, so every desk page redirects to it and no spec can load a form,
    # and no Company exists, so any test that creates a Project fails with "company: mandatory".
    frappe.db.set_value("Installed Application", {"app_name": ["in", ["frappe", "erpnext"]]}, "is_setup_complete", 1)
    setup = {"setup_complete": True, "company": None, "company_error": None}
    if "erpnext" in frappe.get_installed_apps():
        try:
            existing = frappe.db.get_all("Company", pluck="name", limit=1)
            if not existing:
                # ignore_chart_of_accounts skips the default accounts and warehouses, which hit a known one_fm Warehouse bug.
                frappe.flags.ignore_chart_of_accounts = True
                company = frappe.get_doc({"doctype": "Company", "company_name": "One Facilities Management", "abbr": "ONEFM",
                                          "default_currency": "KWD", "country": "Kuwait"}).insert(ignore_permissions=True)
                frappe.db.set_default("company", company.name)
                existing = [company.name]
            setup["company"] = existing[0]
        except Exception as exc:  # the wizard flag above is what unblocks the forms, so a Company problem is reported, not fatal
            frappe.db.rollback()
            setup["company_error"] = f"{type(exc).__name__}: {exc}"[:300]
    frappe.db.commit()
    out = []
    for u in users:
        if u["role"] and not frappe.db.exists("Role", u["role"]):
            raise Exception("role does not exist: " + u["role"])
        created = not frappe.db.exists("User", u["email"])
        if created:
            doc = frappe.new_doc("User")
            doc.update({"email": u["email"], "first_name": u["first_name"], "send_welcome_email": 0, "user_type": "System User"})
        else:
            doc = frappe.get_doc("User", u["email"])
        doc.enabled = 1
        doc.new_password = password
        doc.set("roles", [])
        if u["role"]:
            doc.append("roles", {"role": u["role"]})
        doc.save(ignore_permissions=True)
        out.append({"email": u["email"], "role": u["role"], "created": created})
    frappe.db.commit()
finally:
    frappe.destroy()
print("SETUP_JSON=" + json.dumps(setup))
print("USERS_JSON=" + json.dumps(out))
"""


def _user_plan(run_id, roles):
    """One user per unique role, plus one with none of them. Same run + role gives the same email, so a repeat finds it."""
    plan, seen = [], set()
    for role in dict.fromkeys(roles):
        slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
        if slug in seen:
            raise ValueError(f"role {role!r} collides with another role after normalising")
        seen.add(slug)
        plan.append({"email": f"{slug}.{run_id}@sandbox.test", "role": role, "first_name": f"Test {role}"})
    plan.append({"email": f"norole.{run_id}@sandbox.test", "role": None, "first_name": "Test NoRole"})
    return plan


def _validate_prepare_site(target_app, payload):
    roles, password = payload.get("roles"), payload.get("password")
    error = (
        _check_run_id(payload.get("run_id")) or _check_branch(payload.get("base_branch"))
        or _check_pr_number(payload.get("pr_number")) or _check_app(target_app)
    )
    if error:
        return error
    if not isinstance(roles, list) or not roles or not all(isinstance(r, str) and _ROLE_NAME.match(r) for r in roles):
        return "roles must be a non-empty list of valid role names."
    if not isinstance(password, str) or not 8 <= len(password) <= _PASSWORD_MAX or password != password.strip():
        return "password must be a string of 8 to 128 characters without surrounding whitespace."
    return None


def prepare_site(target_app, payload):
    """AC1: base branch checked out, site migrated, ERPNext setup marked complete with one default Company,
    one test user per role plus one with none, mute_emails on, then a mysqldump of the site DB under SNAPSHOT_DIR. Returns a result dict; never contains the password."""
    if isinstance(payload.get("roles"), str):  # a Processa text field: one role per line or comma-separated
        payload = {**payload, "roles": [r.strip() for r in re.split(r"[,\n]", payload["roles"]) if r.strip()]}
    error = _validate_prepare_site(target_app, payload)
    if error:
        return _failed("validate", error)
    run_id, base_branch, password = payload["run_id"], payload["base_branch"], payload["password"]
    try:
        plan = _user_plan(run_id, payload["roles"])
    except ValueError as exc:
        return _failed("validate", str(exc))
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    ok, err = _checkout_target_branch(target_app, base_branch)
    if not ok:
        return _failed("checkout_base", err)
    base_sha = _exec(["git", "rev-parse", "HEAD"], cwd=app_dir).stdout.strip()
    if not _SHA.match(base_sha):
        return _failed("checkout_base", "could not resolve the base commit")

    migrate = _exec(["bench", "--site", SITE_NAME, "migrate"], cwd=BENCH_DIR, timeout=1800)
    if migrate.returncode != 0:
        return _failed("migrate", "migration failed on the base branch", migration_tail=_tail(migrate, password))

    env = {**os.environ, "SANDBOX_TEST_USERS": json.dumps(plan), "SANDBOX_TEST_PASSWORD": password, "SANDBOX_SITE": SITE_NAME}
    users = _exec([f"{BENCH_DIR}/env/bin/python", "-c", _ENSURE_USERS_SCRIPT], env=env, cwd=f"{BENCH_DIR}/sites", timeout=300)
    marker = next((l for l in (users.stdout or "").splitlines() if l.startswith("USERS_JSON=")), None)
    if users.returncode != 0 or not marker:
        return _failed("create_users", "creating the test users failed", detail=_tail(users, password))
    created = json.loads(marker[len("USERS_JSON="):])
    setup_line = next((l for l in (users.stdout or "").splitlines() if l.startswith("SETUP_JSON=")), None)
    setup = json.loads(setup_line[len("SETUP_JSON="):]) if setup_line else None

    muted = _exec(["bench", "--site", SITE_NAME, "set-config", "mute_emails", "1"], cwd=BENCH_DIR, timeout=120)
    if muted.returncode != 0:
        return _failed("mute_emails", "could not set mute_emails", detail=_tail(muted, password))

    try:
        sql_path, marker_path = _snapshot_paths(run_id)
        os.makedirs(SNAPSHOT_DIR, mode=0o700, exist_ok=True)
        db_name, db_password, db_host, db_port = _site_db_credentials()
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        return _failed("snapshot", f"{type(exc).__name__}: {exc}")
    part = sql_path + ".part"
    dump = _exec(
        ["mysqldump", "--single-transaction", "--routines", "--triggers", "-h", db_host, "-P", db_port, "-u", db_name,
         f"--result-file={part}", db_name],
        env={**os.environ, "MYSQL_PWD": db_password}, timeout=1800,
    )
    if dump.returncode != 0 or not os.path.isfile(part) or os.path.getsize(part) == 0:
        with contextlib.suppress(OSError):
            os.remove(part)
        return _failed("snapshot", "mysqldump failed or produced an empty file", detail=_tail(dump, db_password, password))
    os.replace(part, sql_path)  # only this run's own file name is ever replaced
    size = os.path.getsize(sql_path)
    with open(marker_path, "w") as fh:
        json.dump({"run_id": run_id, "base_branch": base_branch, "base_sha": base_sha, "pr_number": int(payload["pr_number"]),
                   "target_app": target_app, "snapshot": sql_path,
                   "users": [{"email": u["email"], "role": u["role"]} for u in created]}, fh)
    # The spec steps log in as these users. The file is for this container only, owner-read, and never logged or returned.
    with os.fdopen(os.open(sql_path[:-4] + ".pw", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(password)
    return {"status": "prepared", "run_id": run_id, "base_branch": base_branch, "base_sha": base_sha, "users": created,
            "mute_emails": True, "setup": setup, "snapshot_path": sql_path, "snapshot_bytes": size}


def _frontend_changed(target_app, paths):
    """Only the app's public/ assets or its own frontend folder need a bundle rebuild; a backend change does not."""
    public = re.compile(rf"\A{re.escape(target_app)}/public/")
    return any(public.match(p) or p.startswith(_FRONTEND_DIRS.get(target_app, ())) for p in paths)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_services(pids):
    import signal
    for pid in pids:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while any(_pid_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid in pids:
        if _pid_alive(pid):
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)


def _ping(port=SERVE_PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/method/ping", timeout=5) as response:
            return response.status == 200 and json.loads(response.read()).get("message") == "pong"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _start_services():
    """One bench serve on 8000 and one worker, each in its own session so they outlive the command.
    Services from an earlier call are stopped first, so there is never a second set. Returns (pids, error)."""
    state_file = os.path.join(BENCH_DIR, "logs", "sandbox_services.json")
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    try:
        with open(state_file) as fh:
            old = [p for p in json.load(fh).get("pids", []) if isinstance(p, int) and _pid_alive(p)]
    except (OSError, ValueError):
        old = []
    _stop_services(old)
    if _ping():
        return None, f"port {SERVE_PORT} is already answering and is not a service this agent started"
    pids = []
    for name, args in (("serve", ["bench", "--site", SITE_NAME, "serve", "--port", str(SERVE_PORT)]),
                       ("worker", ["bench", "worker", "--queue", "short,default,long"])):
        with open(os.path.join(BENCH_DIR, "logs", f"sandbox_{name}.log"), "ab") as log:
            try:
                proc = _spawn(args, cwd=BENCH_DIR, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                _stop_services(pids)
                return None, f"could not start {name}: {exc}"
        pids.append(proc.pid)
        with open(state_file, "w") as fh:
            json.dump({"pids": pids}, fh)
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not all(_pid_alive(p) for p in pids):
            break
        if _ping():
            return pids, None
        time.sleep(2)
    _stop_services(pids)
    with open(state_file, "w") as fh:
        json.dump({"pids": []}, fh)
    log_tail = ""
    with contextlib.suppress(OSError), open(os.path.join(BENCH_DIR, "logs", "sandbox_serve.log"), "rb") as fh:
        log_tail = fh.read()[-1500:].decode(errors="replace")
    return None, f"the site did not answer /api/method/ping on port {SERVE_PORT}: {log_tail}"


# --- Testing Agent: the mobile app as a web app against the sandbox site -----------------------
# mobile_app_ionic is not a Frappe app, so its pull request is built with vite instead of `bench build`, pointed at the
# sandbox site and served with `vite preview`. VITE_BASE_URL is the router's base PATH in that app
# (createWebHistory(import.meta.env.VITE_BASE_URL)), which every .env file keeps as "/": it is not an address, and a
# site URL there breaks every route, so only VITE_BASE_API_URL points at the sandbox.
MOBILE_APP = "mobile_app_ionic"
# Target apps that are plain Python projects: their tests are run with unittest, not bench.
PYTHON_TEST_APPS = ("agent_sandbox",)
MOBILE_PORT = 8100
MOBILE_URL = f"http://127.0.0.1:{MOBILE_PORT}"
MOBILE_API_PREFIX = "/api/method/one_fm.api."
_PRODUCTION_HOST = re.compile(rb"[\w:/.@-]*one-fm\.com[\w/.\-]*", re.I)
# The only mentions of the production host that are allowed in a build: a link the app opens for a person to read, never one it
# calls. DataUsePage.vue hard-codes the privacy policy address, so without this every build would fail. Add to it deliberately.
_PRODUCTION_LINKS_ALLOWED = ("https://app.one-fm.com/privacy_policy",)
_CONSENT_VERSION = re.compile(r"DATA_USE_POLICY_VERSION\s*=\s*[\"']([^\"']+)[\"']")


def _http_ok(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _write_sandbox_env(app_dir):
    """.env.sandbox, copied from .env.staging (not .env.example, whose API prefix lacks the trailing dot) with the API
    pointed at the sandbox site. Returns an error string, or None."""
    try:
        with open(os.path.join(app_dir, ".env.staging")) as fh:
            lines = [l.rstrip("\n") for l in fh]
    except OSError:
        return ".env.staging is missing from the app, so there is nothing to build the sandbox environment from"
    overrides = {"VITE_BASE_API_URL": f"http://127.0.0.1:{SERVE_PORT}", "VITE_API_PREFIX": MOBILE_API_PREFIX}
    seen, out = set(), []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in overrides and not line.lstrip().startswith("#"):
            out.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in overrides.items() if k not in seen]
    with open(os.path.join(app_dir, ".env.sandbox"), "w") as fh:
        fh.write("\n".join(out) + "\n")
    return None


def _production_references(dist):
    """(relative path, snippet) for every built file that mentions the production host."""
    found = []
    for root, _dirs, files in os.walk(dist):
        for name in files:
            path = os.path.join(root, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            for match in _PRODUCTION_HOST.finditer(data):
                mention = match.group(0).decode(errors="replace")
                if not mention.lower().startswith(tuple(_PRODUCTION_LINKS_ALLOWED)):
                    found.append((os.path.relpath(path, dist), mention))
                    break
    return found


def _build_mobile(app_dir):
    """Install, build with --mode sandbox, and refuse a build that mentions production. yarn.lock is put back whatever happens,
    so the install never reaches the pull request. Returns None, or a failure result."""
    try:
        if _exec(["yarn", "check", "--integrity"], cwd=app_dir, timeout=300).returncode != 0:
            install = _exec(["yarn", "install"], cwd=app_dir, timeout=1800)
            if install.returncode != 0:
                return _failed("install", "yarn install failed", build_tail=_tail(install))
        if error := _write_sandbox_env(app_dir):
            return _failed("env", error)
        build = _exec(["yarn", "vite", "build", "--mode", "sandbox"], cwd=app_dir, timeout=1800)
        if build.returncode != 0:
            return _failed("build", "the mobile web build failed", build_tail=_tail(build))
        dist = os.path.join(app_dir, "dist")
        if not os.path.isdir(dist):
            return _failed("build", "the build produced no dist folder", build_tail=_tail(build))
        if found := _production_references(dist):
            return _failed("production_url", f"{len(found)} built file(s) mention one-fm.com, so tests could reach production: "
                           + "; ".join(f"{path} ({mention!r})" for path, mention in found[:5]), files=[path for path, _ in found][:20])
        return None
    finally:
        _exec(["git", "checkout", "--", "yarn.lock"], cwd=app_dir)


def _consent_version(app_dir):
    """The data-use policy version the app is checked out at; the stored consent only counts when it equals this."""
    try:
        with open(os.path.join(app_dir, "src", "composable", "useDataUseConsent.js")) as fh:
            found = _CONSENT_VERSION.search(fh.read())
        return found.group(1) if found else None
    except OSError:
        return None


def _start_preview(app_dir, state_file):
    """vite preview on the mobile port, tracked beside the site's own processes so the next run stops it too."""
    log_path = os.path.join(BENCH_DIR, "logs", "sandbox_preview.log")
    with open(log_path, "ab") as log:
        try:
            proc = _spawn(["yarn", "vite", "preview", "--host", "127.0.0.1", "--port", str(MOBILE_PORT), "--strictPort"], cwd=app_dir,
                          stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            return None, f"could not start vite preview: {exc}"
    try:
        with open(state_file) as fh:
            pids = json.load(fh).get("pids", [])
    except (OSError, ValueError):
        pids = []
    with open(state_file, "w") as fh:
        json.dump({"pids": [*pids, proc.pid]}, fh)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not _pid_alive(proc.pid):
            break
        if _http_ok(MOBILE_URL + "/"):
            return proc.pid, None
        time.sleep(2)
    _stop_services([proc.pid])
    tail = ""
    with contextlib.suppress(OSError), open(log_path, "rb") as fh:
        tail = fh.read()[-800:].decode(errors="replace")
    return None, f"the mobile preview did not answer on {MOBILE_URL}: {tail}"


def checkout_pr_head(target_app, payload):
    """AC2-AC5: fetch refs/pull/<N>/head, check out that exact commit, rebuild assets only for frontend changes,
    migrate, and only then start serve + one worker and wait for /api/method/ping. Fetch and checkout only."""
    error = _check_run_id(payload.get("run_id")) or _check_pr_number(payload.get("pr_number")) or _check_app(target_app)
    if error:
        return _failed("validate", error)
    run_id, number = payload["run_id"], int(payload["pr_number"])
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    try:
        sql_path, marker_path = _snapshot_paths(run_id)
        with open(marker_path) as fh:
            prepared = json.load(fh)
        assert os.path.getsize(sql_path) > 0 and prepared["pr_number"] == number and prepared["target_app"] == target_app
    except (OSError, ValueError, KeyError, AssertionError):
        return _failed("verify_base", "the site was not prepared for this run and pull request (run prepare_site first)")

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    with _authed_remote(app_dir, github_token):
        fetch = _exec(["git", "fetch", "origin", f"refs/pull/{number}/head"], cwd=app_dir, timeout=600)
    if fetch.returncode != 0:
        return _failed("fetch", f"git fetch of refs/pull/{number}/head failed", detail=_tail(fetch, github_token))
    sha = _exec(["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=app_dir).stdout.strip()
    if not _SHA.match(sha):
        return _failed("fetch", "could not resolve the fetched pull request head")
    checkout = _exec(["git", "checkout", "-f", "--detach", sha], cwd=app_dir)
    if checkout.returncode != 0 or _exec(["git", "rev-parse", "HEAD"], cwd=app_dir).stdout.strip() != sha:
        return _failed("checkout", f"git checkout of {sha} failed", detail=_tail(checkout, github_token))

    diff = _exec(["git", "diff", "--name-only", prepared["base_sha"], sha], cwd=app_dir)
    if diff.returncode != 0:
        return _failed("diff", "git diff of base against the pull request head failed", detail=_tail(diff, github_token))
    paths = [p for p in diff.stdout.splitlines() if p]
    mobile = target_app == MOBILE_APP
    built = True if mobile else _frontend_changed(target_app, paths)
    if mobile:  # a web app, not a Frappe app: nothing to bench build or migrate, so it is built with vite and served beside the site
        if failure := _build_mobile(app_dir):
            return {**failure, "pr_head_sha": sha}
        cors = _exec(["bench", "--site", SITE_NAME, "set-config", "allow_cors", MOBILE_URL], cwd=BENCH_DIR, timeout=120)
        if cors.returncode != 0:
            return _failed("cors", "could not allow the preview origin on the sandbox site", detail=_tail(cors), pr_head_sha=sha)
    else:
        if built:
            build = _exec(["bench", "build", "--app", target_app], cwd=BENCH_DIR, timeout=3600)
            if build.returncode != 0:
                return _failed("build", "bench build failed", build_tail=_tail(build), pr_head_sha=sha)
        migrate = _exec(["bench", "--site", SITE_NAME, "migrate"], cwd=BENCH_DIR, timeout=1800)
        if migrate.returncode != 0:
            return _failed("migrate", "migration failed on the pull request head", migration_tail=_tail(migrate), pr_head_sha=sha)

    pids, error = _start_services()
    if error:
        return _failed("start_services", error, pr_head_sha=sha)
    record = {**prepared, "head_sha": sha, "built_assets": built}
    extra = {}
    if mobile:
        preview_pid, error = _start_preview(app_dir, os.path.join(BENCH_DIR, "logs", "sandbox_services.json"))
        if error:
            return _failed("start_preview", error, pr_head_sha=sha)
        record["mobile"] = {"url": MOBILE_URL, "consent_version": _consent_version(app_dir)}
        extra = {"mobile_url": MOBILE_URL, "preview_pid": preview_pid}
    with open(marker_path, "w") as fh:
        json.dump(record, fh)
    return {"status": "ready", "run_id": run_id, "pr_number": number, "pr_head_sha": sha, "base_sha": prepared["base_sha"],
            "changed_files": len(paths), "built_assets": built, "port": SERVE_PORT, "serve_pid": pids[0], "worker_pid": pids[1],
            "ping": "pong", **extra}


# --- Testing Agent: writing and running Playwright specs ---------------------------------------
# Specs live outside every app checkout, so the file tools' commit-and-push never sees them, and nothing here touches git
# except checking the base commit out. Videos stay in the container until upload_videos removes them.
SPECS_DIR = os.environ.get("SPECS_DIR", "/home/frappe/specs")
SPEC_FILE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.spec\.(?:js|mjs|ts)\Z")
SPEC_MAX_BYTES = 100_000
SPEC_TIMEOUT_SECONDS = 1800
SNAPSHOT_TIMEOUT_SECONDS = 60
_RESULT_MAX_CHARS = 900_000  # a command result is capped at 1 MB
_NOT_PREPARED = "prepare the site first: run prepare_site, then checkout_pr_head, for this run"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_METHOD = re.compile(r"\A[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\Z")

# Written by the sandbox, not by the model, so a spec file cannot weaken it. Every spec runs in Chromium with a 1280x720
# webm; specs tagged @ui also run at 390x844. A spec that fails and then passes on the one rerun is reported "flaky".
# Written into every run folder by the sandbox, so a spec the model writes calls these instead of inventing its own login and
# waits. They follow Frappe v15's own Playwright helpers (frappe/playwright/support): log in with POST /api/method/login and
# keep the session, wait for frappe.request.ajax_count === 0 and body[data-ajax-state="complete"], select fields with
# [data-fieldname=...], type into Link fields with pressSequentially and wait for the dropdown, wait for a dialog to finish
# fading in, and blur a field before clicking Save. Plain JavaScript: it needs no Playwright import of its own.
_FRAPPE_HELPERS = """// frappe-helpers.js: written by the sandbox before every run. Do not edit it; use it:
//   const h = require('./frappe-helpers');
//   await h.loginAs(page, '<a test user email from prepare_site>');
//   await h.openForm(page, 'Work Item');  await h.fillField(page, 'title', 'x');  await h.saveForm(page);
const BASE = process.env.SPEC_BASE_URL;

const slug = (doctype) => String(doctype).toLowerCase().replace(/ /g, '-');

// Log in through the API; the page keeps the session cookie. The password comes from the environment, never from a spec.
async function loginAs(page, email) {
  const res = await page.request.post(BASE + '/api/method/login', { form: { usr: email, pwd: process.env.TEST_PASSWORD } });
  if (!res.ok()) throw new Error('login as ' + email + ' failed: HTTP ' + res.status());
}

// Wait until Frappe has nothing in flight. Pages that are not Frappe's (no window.frappe) are treated as idle.
async function waitForFrappeIdle(page) {
  await page.waitForFunction(() => !window.frappe || !frappe.request ||
    (frappe.request.ajax_count === 0 && document.body.getAttribute('data-ajax-state') !== 'running'), null, { timeout: 30000 });
}

async function openForm(page, doctype, name) {
  await page.goto('/app/' + slug(doctype) + '/' + (name ? encodeURIComponent(name) : 'new'));
  await waitForFrappeIdle(page);
}

async function openList(page, doctype) {
  await page.goto('/app/' + slug(doctype));
  await waitForFrappeIdle(page);
}

// The input of a form field, found by its data-fieldname.
function field(page, fieldname) {
  return page.locator('[data-fieldname="' + fieldname + '"] :is(input, textarea, select):visible').first();
}

async function fillField(page, fieldname, value) {
  const input = field(page, fieldname);
  await input.fill(String(value));
  await input.blur();
  await waitForFrappeIdle(page);
}

// Type into a Link field and pick from its dropdown once it has appeared.
async function fillLink(page, fieldname, text) {
  const input = field(page, fieldname);
  await input.click();
  await input.pressSequentially(String(text), { delay: 50 });
  const option = page.locator('[data-fieldname="' + fieldname + '"] .awesomplete ul li:visible').filter({ hasText: String(text) }).first();
  await option.waitFor({ state: 'visible', timeout: 15000 });
  await option.click();
  await waitForFrappeIdle(page);
}

// A dialog is ready only after its fade-in has finished.
async function waitForDialog(page) {
  await page.locator('.modal.show').last().waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForFunction(() => !document.querySelector('.modal.fade:not(.show)') || document.querySelector('.modal.show'), null, { timeout: 15000 });
}

// Blur whatever has focus, then click Save, then wait for Frappe to finish.
async function saveForm(page) {
  await page.evaluate(() => document.activeElement && document.activeElement.blur && document.activeElement.blur());
  await waitForFrappeIdle(page);
  await page.locator('.page-actions .primary-action:visible').first().click();
  await waitForFrappeIdle(page);
}

module.exports = { loginAs, waitForFrappeIdle, openForm, openList, field, fillField, fillLink, waitForDialog, saveForm };
"""


_SPEC_CONFIG = """const { defineConfig } = require('@playwright/test');
const out = process.env.SPEC_OUTPUT_DIR;
module.exports = defineConfig({
  testDir: __dirname, outputDir: out + '/artifacts', reporter: [['json', { outputFile: out + '/report.json' }]],
  workers: 1, retries: 1, timeout: 60000,
  use: { baseURL: process.env.SPEC_BASE_URL, trace: 'off', launchOptions: { args: ['--no-sandbox', '--disable-dev-shm-usage'] } },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1280, height: 720 }, video: { mode: 'on', size: { width: 1280, height: 720 } } } },
    { name: 'mobile', grep: /@ui/, use: { viewport: { width: 390, height: 844 }, video: { mode: 'on', size: { width: 390, height: 844 } } } },
  ],
});
"""

# Logs in with POST /api/method/login (as frappe's own Playwright helpers do) and returns the page's accessibility snapshot
# and a screenshot. The user and password come from the environment, never from argv.
_SNAPSHOT_SCRIPT = """const { chromium } = require('@playwright/test');
(async () => {
  const base = process.env.SPEC_BASE_URL;
  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const context = await browser.newContext({ baseURL: base, viewport: { width: 1280, height: 720 } });
    const login = await context.request.post(base + '/api/method/login', { form: { usr: process.env.SNAP_USER, pwd: process.env.SNAP_PASSWORD } });
    if (!login.ok()) throw new Error('login failed: ' + login.status());
    const page = await context.newPage();
    page.setDefaultTimeout(45000);
    await page.goto(process.env.SNAP_URL);
    // The desk draws a page in steps (header first, then the form after its document loads), so no single selector or
    // request count says it is done. Let the network settle, then sample the page every half second until it has not changed
    // for two seconds (at most 15 s). Each wait is best effort: a page that never settles is still snapshotted as it is.
    await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
    let last = '', unchanged = 0;
    for (let waited = 0; waited < 15000 && unchanged < 4; waited += 500) {
      const now = await page.locator('body').ariaSnapshot().catch(() => '');
      unchanged = now === last && now ? unchanged + 1 : 0;
      last = now;
      await page.waitForTimeout(500);
    }
    const snapshot = await page.locator('body').ariaSnapshot();
    const shot = await page.screenshot({ type: 'jpeg', quality: 60 });
    process.stdout.write('SNAPSHOT_JSON=' + JSON.stringify({ snapshot: snapshot.slice(0, 200000), screenshot_base64: shot.toString('base64') }));
  } finally { await browser.close(); }
})().catch((e) => { console.error(String(e).slice(0, 500)); process.exit(2); });
"""

# Runs inside the bench virtualenv. Refuses before running anything unless the method is one of the target app's own
# scheduled methods.
_TRIGGER_SCRIPT = """
import json, os, frappe
frappe.init(site=os.environ["SANDBOX_SITE"], sites_path=".")
frappe.connect()
try:
    events = frappe.get_hooks("scheduler_events", app_name=os.environ["JOB_APP"]) or {}
    allowed = set()
    for value in events.values():
        for item in (value.values() if isinstance(value, dict) else [value]):
            allowed.update(item if isinstance(item, (list, tuple)) else [item])
    method = os.environ["JOB_METHOD"]
    if method not in allowed:
        print("JOB_JSON=" + json.dumps({"refused": True}))
    else:
        frappe.set_user("Administrator")
        frappe.get_attr(method)()
        frappe.db.commit()
        print("JOB_JSON=" + json.dumps({"ran": True}))
finally:
    frappe.destroy()
"""


def _spec_run_dir(run_id):
    root = os.path.realpath(SPECS_DIR)
    path = os.path.realpath(os.path.join(root, run_id))
    if os.path.dirname(path) != root:
        raise ValueError("spec folder escapes the specs directory")
    return path


def write_spec(target_app, args):
    """AC1: write one spec under /home/frappe/specs/<run>/. Never in an app checkout, so never committed or pushed."""
    run_id, name, content = args.get("run_id"), args.get("file"), args.get("content")
    error = _check_run_id(run_id)
    if error:
        return {"error": error}
    if not isinstance(name, str) or not SPEC_FILE.match(name):
        return {"error": f"file {name!r} must be a plain file name ending in .spec.js, .spec.mjs or .spec.ts."}
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > SPEC_MAX_BYTES:
        return {"error": f"content must be non-empty text of at most {SPEC_MAX_BYTES} bytes."}
    run_dir = _spec_run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, name), "w") as fh:
        fh.write(content)
    return {"written": f"{run_id}/{name}", "bytes": len(content.encode())}


def _load_prepared(run_id):
    """The run's marker, or None when prepare_site has not run for it."""
    try:
        sql_path, marker_path = _snapshot_paths(run_id)
        with open(marker_path) as fh:
            marker = json.load(fh)
        return marker if os.path.getsize(sql_path) > 0 else None
    except (OSError, ValueError, KeyError):
        return None


def _spec_env(run_id, run_dir, code="head"):
    pw_path = _snapshot_paths(run_id)[0][:-4] + ".pw"
    with open(pw_path) as fh:
        password = fh.read()
    env = {**os.environ, "SPEC_BASE_URL": f"http://127.0.0.1:{SERVE_PORT}", "SPEC_OUTPUT_DIR": os.path.join(run_dir, f"out-{code}"),
           "TEST_PASSWORD": password}
    mobile = (_load_prepared(run_id) or {}).get("mobile")
    if mobile:  # the web build of the app is on its own port; specs open it with openApp() from mobile-helpers.js
        env.update({"SPEC_MOBILE_URL": mobile["url"], "SPEC_MOBILE_CONSENT_VERSION": mobile.get("consent_version") or ""})
    return env


def _criterion(*texts):
    for text in texts:
        found = re.search(r"\bAC\s*-?(\d+)\b", text or "", re.I)
        if found:
            return f"AC{found.group(1)}"
    return None


_TEST_STATUS = {"expected": "passed", "unexpected": "failed", "flaky": "flaky", "skipped": "skipped"}


def _parse_report(report):
    """One entry per spec and viewport: file, criterion id, status, failing assertion, duration and video path."""
    results = []

    def walk(suite, file):
        file = suite.get("file") or file
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                runs = test.get("results") or []
                failing = next((r.get("error", {}).get("message") for r in runs if r.get("error")), None)
                video = next((a.get("path") for r in reversed(runs) for a in r.get("attachments", []) if a.get("name") == "video"), None)
                results.append({
                    "file": spec.get("file") or file, "criterion": _criterion(spec.get("title"), spec.get("file") or file),
                    "title": spec.get("title"), "viewport": "mobile" if test.get("projectName") == "mobile" else "desktop",
                    "status": _TEST_STATUS.get(test.get("status"), "failed"),
                    "failing_assertion": _ANSI.sub("", failing).strip()[:500] if failing else None,
                    "duration_ms": int(sum(r.get("duration", 0) for r in runs)), "video": video,
                })
        for child in suite.get("suites", []):
            walk(child, file)

    for suite in report.get("suites", []):
        walk(suite, None)
    return results


# Written into the run folder for mobile_app_ionic runs. The app keeps its session in localStorage (the pinia stores `user` and
# `auth`, whose guard needs a user and a token) and its data-use consent as CapacitorStorage.data_use_consent, which only counts
# when it equals the app's current policy version (read from the source at checkout, as SPEC_MOBILE_CONSENT_VERSION). To seed a
# session it makes the app's own login call, so the stored user is exactly what the app would have stored; the password
# comes from the caller and is never put in an error message.
_MOBILE_HELPERS = """// mobile-helpers.js: written by the sandbox for mobile_app_ionic runs. Do not edit it; use it:
//   const m = require('./mobile-helpers');
//   await m.seedSession(page, { employeeId, password });  await m.openApp(page, '/home');
const MOBILE = process.env.SPEC_MOBILE_URL;
const API = process.env.SPEC_BASE_URL;

// The app's own login call. No cookie jar is kept, because the app signs requests with the token only.
async function apiLogin(employeeId, password) {
  const res = await fetch(API + '/api/method/one_fm.api.v1.authentication.user_login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
    body: new URLSearchParams({ employee_id: String(employeeId), password: String(password) }),
  });
  const body = await res.json().catch(() => ({}));
  const data = body.data || (body.message && (body.message.data || body.message));
  if (!res.ok || !data || !data.token) throw new Error('mobile login failed: HTTP ' + res.status);
  return data;
}

// The data-use notice is shown until the stored consent equals the app's current policy version.
async function allowDataUse(page) {
  await page.addInitScript((version) => { if (version) localStorage.setItem('CapacitorStorage.data_use_consent', version); }, process.env.SPEC_MOBILE_CONSENT_VERSION || '');
}

// Put the user, the token and the consent in localStorage before the app loads, so it opens signed in on the home screen.
// Give { employeeId, password } to log in through the API, or { user, token } to use a token you already have
// (the Bearer token from user_login, or 'token <api_key>:<api_secret>').
async function seedSession(page, who) {
  const data = who.token && who.user ? { ...who.user, token: who.token } : await apiLogin(who.employeeId, who.password);
  const user = { user: data, token: data.token, refreshToken: data.refresh_token || null, isEndpointEnabled: 1 };
  const auth = { employeeId: data.employee_id || who.employeeId || null, isRegistered: true };
  await page.addInitScript(([u, a]) => { localStorage.setItem('user', u); localStorage.setItem('auth', a); }, [JSON.stringify(user), JSON.stringify(auth)]);
  await allowDataUse(page);
  return data;
}

async function openApp(page, path) {
  await page.goto(MOBILE + (path || '/'));
  await page.waitForLoadState('networkidle');
}

module.exports = { apiLogin, allowDataUse, seedSession, openApp };
"""

# The one smoke spec: log in through the Employee ID screen and the password screen and reach the home screen. It skips, with its
# reason, until a seeded employee is given in SPEC_MOBILE_EMPLOYEE_ID and SPEC_MOBILE_EMPLOYEE_PASSWORD.
_MOBILE_SMOKE = """// mobile-smoke.spec.js: written by the sandbox for mobile_app_ionic runs.
const { test, expect } = require('@playwright/test');
const m = require('./mobile-helpers');

test('Mobile smoke: the Employee ID and password screens lead to the home screen @ui @mobile-smoke', async ({ page }) => {
  const id = process.env.SPEC_MOBILE_EMPLOYEE_ID, password = process.env.SPEC_MOBILE_EMPLOYEE_PASSWORD;
  test.skip(!id || !password, 'no seeded employee: SPEC_MOBILE_EMPLOYEE_ID and SPEC_MOBILE_EMPLOYEE_PASSWORD are not set');
  await m.allowDataUse(page);
  await m.openApp(page, '/');
  await page.locator('ion-input input').first().fill(id);
  await page.getByRole('button', { name: /next/i }).click();
  await page.locator('ion-input input[type="password"]').fill(password);
  await page.getByRole('button', { name: /log ?in|sign in|submit/i }).click();
  await expect(page).toHaveURL(/\\/home|\\/dashboard/, { timeout: 30000 });
});
"""

_NATIVE_TAG = "@native-device"
_TEST_TITLE = re.compile(r"""\btest(?:\.\w+)?\(\s*(?P<q>['"`])(?P<title>.*?)(?P=q)""")


def _native_device_results(run_dir):
    """A criterion that needs the camera, face enrollment, push or another native feature cannot be tested in a browser. Its
    spec is tagged @native-device, is not run, and is reported as Could not test with the reason native_device."""
    found = []
    for name in sorted(os.listdir(run_dir)):
        if not SPEC_FILE.match(name):
            continue
        with open(os.path.join(run_dir, name)) as fh:
            text = fh.read()
        for match in _TEST_TITLE.finditer(text):
            if _NATIVE_TAG in match.group("title"):
                found.append({"file": name, "criterion": _criterion(match.group("title"), name), "title": match.group("title"),
                              "viewport": "mobile", "status": "could_not_test", "reason": "native_device", "failing_assertion": None,
                              "duration_ms": 0, "video": None})
    return found


def _run_playwright(run_id, run_dir, extra_args, code="head"):
    """Run the run folder's specs. Returns (results, error). The head and the base each keep their own output folder and
    results file, so running the base never deletes the head's videos that upload_videos still has to send."""
    out = os.path.join(run_dir, f"out-{code}")
    shutil.rmtree(out, ignore_errors=True)
    with open(os.path.join(run_dir, "playwright.config.js"), "w") as fh:
        fh.write(_SPEC_CONFIG)
    with open(os.path.join(run_dir, "frappe-helpers.js"), "w") as fh:  # rewritten every run, so a spec cannot change it
        fh.write(_FRAPPE_HELPERS)
    if (_load_prepared(run_id) or {}).get("mobile"):
        for name, text in (("mobile-helpers.js", _MOBILE_HELPERS), ("mobile-smoke.spec.js", _MOBILE_SMOKE)):
            with open(os.path.join(run_dir, name), "w") as fh:
                fh.write(text)
    native = _native_device_results(run_dir)
    run = _exec(["npx", "playwright", "test", "--config", "playwright.config.js", "--grep-invert", _NATIVE_TAG, *extra_args],
                env=_spec_env(run_id, run_dir, code), cwd=run_dir, timeout=SPEC_TIMEOUT_SECONDS)
    report_path = os.path.join(out, "report.json")
    if not os.path.isfile(report_path) and native and "No tests found" in f"{run.stdout}{run.stderr}":
        results = []  # every spec was native-only, so nothing ran and there is nothing else to report
    elif run.returncode not in (0, 1) or not os.path.isfile(report_path):  # 1 only means some spec failed
        return None, f"playwright did not produce a report (exit {run.returncode}): {_tail(run, _spec_env(run_id, run_dir)['TEST_PASSWORD'])}"
    else:
        with open(report_path) as fh:
            results = _parse_report(json.load(fh))
    results = results + native
    with open(os.path.join(run_dir, f"results-{code}.json"), "w") as fh:
        json.dump(results, fh)
    return results, None


def _spec_summary(results):
    counts = {}
    for entry in results:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def _spec_result(run_id, results, **extra):
    body = {"status": "specs_run", "run_id": run_id, "results": results, "summary": _spec_summary(results), **extra}
    if len(json.dumps(body)) > _RESULT_MAX_CHARS:
        body["results"] = [{**r, "failing_assertion": (r["failing_assertion"] or "")[:100] or None} for r in results]
    return body


def run_specs(target_app, args):
    """AC2, AC4, AC7: run the run folder's specs against the PR head site."""
    run_id = args.get("run_id")
    error = _check_run_id(run_id) or _check_app(target_app)
    if error:
        return _failed("validate", error)
    if not _load_prepared(run_id) or not _ping():  # also covers a head site that was never started
        return _failed("not_prepared", _NOT_PREPARED)
    run_dir = _spec_run_dir(run_id)
    if not (os.path.isdir(run_dir) and any(SPEC_FILE.match(f) for f in os.listdir(run_dir))):
        return _failed("no_specs", "no specs were written for this run: call write_spec first")
    results, error = _run_playwright(run_id, run_dir, [], "head")
    if error:
        return _failed("run", error)
    return _spec_result(run_id, results, code="head")


def run_specs_on_base(target_app, args):
    """AC3: restore the snapshot, check the base code out, restart the server, run only the changed-behavior specs."""
    run_id = args.get("run_id")
    error = _check_run_id(run_id) or _check_app(target_app)
    if error:
        return _failed("validate", error)
    marker = _load_prepared(run_id)
    if not marker or "head_sha" not in marker:
        return _failed("not_prepared", _NOT_PREPARED)
    run_dir = _spec_run_dir(run_id)
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    state_file = os.path.join(BENCH_DIR, "logs", "sandbox_services.json")
    try:
        with open(state_file) as fh:
            _stop_services([p for p in json.load(fh).get("pids", []) if isinstance(p, int) and _pid_alive(p)])
        db_name, db_password, db_host, db_port = _site_db_credentials()
        with open(marker["snapshot"]) as dump:
            restore = _exec(["mysql", "-h", db_host, "-P", db_port, "-u", db_name, db_name], env={**os.environ, "MYSQL_PWD": db_password},
                            timeout=1800, stdin=dump)
    except (OSError, ValueError, RuntimeError) as exc:
        return _failed("restore", f"{type(exc).__name__}: {exc}")
    if restore.returncode != 0:
        return _failed("restore", "restoring the snapshot failed", detail=_tail(restore, db_password))
    checkout = _exec(["git", "checkout", "-f", "--detach", marker["base_sha"]], cwd=app_dir)
    if checkout.returncode != 0 or _exec(["git", "rev-parse", "HEAD"], cwd=app_dir).stdout.strip() != marker["base_sha"]:
        return _failed("checkout_base", f"git checkout of {marker['base_sha']} failed", detail=_tail(checkout))
    if marker.get("built_assets"):  # the head's bundle is still on disk; rebuild so the base is tested as itself
        build = _exec(["bench", "build", "--app", target_app], cwd=BENCH_DIR, timeout=3600)
        if build.returncode != 0:
            return _failed("build", "bench build failed on the base", build_tail=_tail(build))
    _pids, error = _start_services()
    if error:
        return _failed("start_services", error)
    results, error = _run_playwright(run_id, run_dir, ["--grep", "@changed-behavior"], "base")
    if error:
        return _failed("run", error)
    return _spec_result(run_id, results, code="base", base_sha=marker["base_sha"])


def browser_snapshot(target_app, args):
    """AC5: the accessibility snapshot and a screenshot of a page, as a role's test user, within 60 seconds."""
    run_id, url, role = args.get("run_id"), args.get("url"), args.get("role")
    error = _check_run_id(run_id)
    if error:
        return {"error": error}
    marker = _load_prepared(run_id)
    if not marker or not _ping():
        return {"error": _NOT_PREPARED}
    base = f"http://127.0.0.1:{SERVE_PORT}"
    if not isinstance(url, str) or not (url.startswith("/") and not url.startswith("//") or url.startswith(base + "/")):
        return {"error": f"url must be a path on the test site (starting with /) or start with {base}/."}
    user = next((u["email"] for u in marker.get("users", []) if u.get("role") == role), None)
    if not user:
        return {"error": f"role {role!r} has no test user for this run."}
    script_path = os.path.join(_spec_run_dir(run_id), "_snapshot.js")
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, "w") as fh:
        fh.write(_SNAPSHOT_SCRIPT)
    env = _spec_env(run_id, os.path.dirname(script_path))
    env.update({"SNAP_USER": user, "SNAP_PASSWORD": env["TEST_PASSWORD"], "SNAP_URL": url})
    run = _exec(["node", script_path], env=env, cwd=os.path.dirname(script_path), timeout=SNAPSHOT_TIMEOUT_SECONDS)
    line = next((l for l in (run.stdout or "").splitlines() if l.startswith("SNAPSHOT_JSON=")), None) if run.returncode == 0 else None
    if not line:
        return {"error": f"the page could not be captured (exit {run.returncode}): {_tail(run, env['TEST_PASSWORD'])}"}
    return {"url": url, "role": role, **json.loads(line[len("SNAPSHOT_JSON="):])}


def trigger_job(target_app, args):
    """AC6: run one of the target app's scheduled methods, and refuse any method not in its scheduler_events."""
    method = args.get("method")
    error = _check_app(target_app)
    if error or not isinstance(method, str) or not _METHOD.match(method):
        return {"error": error or f"method {method!r} is not a dotted method path."}
    env = {**os.environ, "SANDBOX_SITE": SITE_NAME, "JOB_APP": target_app, "JOB_METHOD": method}
    run = _exec([f"{BENCH_DIR}/env/bin/python", "-c", _TRIGGER_SCRIPT], env=env, cwd=f"{BENCH_DIR}/sites", timeout=600)
    line = next((l for l in (run.stdout or "").splitlines() if l.startswith("JOB_JSON=")), None)
    if run.returncode != 0 or not line:
        return {"error": f"the job could not be run: {_tail(run)}"}
    outcome = json.loads(line[len("JOB_JSON="):])
    if outcome.get("refused"):
        return {"error": f"{method} is not in {target_app}'s scheduler_events, so it is refused."}
    return {"ran": method}


# --- Testing Agent: upload the run's videos for the PR comment ---------------------------------
# GitHub's attachment upload (the call gh makes for `gh pr comment --attach`) refuses the installation tokens of our own Apps
# and accepts a user token, so this uses the machine user's classic token from the payload. The token is never logged,
# returned or written to disk. The endpoint is undocumented: the request below follows gh's own client
# (cli/cli internal/attachments/client.go). If it refuses the token, the machine user posts the whole comment with the
# documented `gh pr comment --attach` instead.
UPLOAD_HOST = "https://uploads.github.com"
GITHUB_API = "https://api.github.com"
UPLOAD_ORG = os.environ.get("UPLOAD_ORG", "ONE-F-M")
VIDEO_MAX_BYTES = 100 * 1024 * 1024
_REPO = re.compile(r"\A[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_REFUSED = (401, 403, 404)  # the endpoint answers 404, not 403, when a token may not write
_DEFAULT_COMMENT = "Testing Agent test videos"


def _http(method, url, token, data=None, headers=None, timeout=300):
    """One request to GitHub. Returns (status, json); status 0 means it never reached the server."""
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"token {token}", "Accept": "application/vnd.github+json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"message": exc.read()[:300].decode(errors="replace")}
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return 0, {"message": f"{type(exc).__name__}: {exc}"}


def _asset_url(answer):
    """The https URL in an upload answer: its url, href or html_url, or the same under asset. None when there is none."""
    for holder in (answer, answer.get("asset") if isinstance(answer.get("asset"), dict) else {}):
        for key in ("url", "href", "html_url"):
            if isinstance(holder.get(key), str) and holder[key].startswith("https://"):
                return holder[key]
    return None


def _videos_by_criterion(run_dir):
    """criterion -> the video to send: the desktop run's, when it left one. None when run_specs has not run."""
    try:
        with open(os.path.join(run_dir, "results-head.json")) as fh:
            results = json.load(fh)
    except (OSError, ValueError):
        return None
    chosen = {}
    for entry in sorted(results, key=lambda r: r.get("viewport") != "desktop"):
        video = entry.get("video")
        key = entry.get("criterion") or entry.get("file")
        if key and video and os.path.isfile(video) and os.path.getsize(video) > 0 and key not in chosen:
            chosen[key] = video
    return chosen


def _delete_videos(run_dir):
    removed = 0
    for folder in (d for d in os.listdir(run_dir) if d.startswith("out-")):
        for root, _dirs, files in os.walk(os.path.join(run_dir, folder)):
            for name in files:
                if name.endswith(".webm"):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(root, name))
                        removed += 1
    return removed


def _comment_with_gh(repo, pr_number, body, videos, token):
    """The fallback: the machine user posts the whole comment through gh, which uploads the files itself."""
    args = ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body]
    for video in videos:
        args += ["--attach", video]
    return _exec(args, env={**os.environ, "GH_TOKEN": token}, timeout=900)


def upload_videos(target_app, args):
    """Upload one video per criterion to the PR's repository and return the user-attachments URLs. Deletes the local videos
    only when every upload worked; a failed upload is reported for its criterion and never counted as a success."""
    run_id, repo, number, token = args.get("run_id"), args.get("repository"), args.get("pr_number"), args.get("token")
    error = _check_run_id(run_id) or _check_pr_number(number) or _check_app(target_app)
    if not error and (not isinstance(repo, str) or not _REPO.match(repo) or repo.split("/")[0] != UPLOAD_ORG):
        error = f"repository {repo!r} must be {UPLOAD_ORG}/<repo>."
    if not error and (not isinstance(token, str) or not token or token != token.strip() or len(token) > 255):
        error = "token is missing or malformed."
    if error:
        return _failed("validate", error)
    run_dir = _spec_run_dir(run_id)
    videos = _videos_by_criterion(run_dir) if os.path.isdir(run_dir) else None
    if not videos:
        return _failed("no_videos", "there are no videos for this run: call run_specs first, and check the specs recorded video")
    body = args.get("comment_body") if isinstance(args.get("comment_body"), str) and args.get("comment_body").strip() else _DEFAULT_COMMENT

    status, repository = _http("GET", f"{GITHUB_API}/repos/{repo}", token, timeout=60)
    unreadable = None
    if status == 200 and repository.get("id"):
        uploads, errors, refused = [], [], False
        for criterion, path in videos.items():
            size = os.path.getsize(path)
            if size > VIDEO_MAX_BYTES:
                errors.append({"criterion": criterion, "error": f"video is {size // (1024 * 1024)} MB, over the 100 MB limit"})
                continue
            query = urllib.parse.urlencode({"name": re.sub(r"[^A-Za-z0-9_.-]", "-", str(criterion)) + ".webm", "content_type": "video/webm",
                                            "repository_id": repository["id"]})
            with open(path, "rb") as fh:
                code, answer = _http("POST", f"{UPLOAD_HOST}/user-attachments/assets?{query}", token, data=fh,
                                     headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})
            if code in (200, 201) and _asset_url(answer):
                uploads.append({"criterion": criterion, "url": _asset_url(answer)})
            elif code in _REFUSED:
                refused = True
                break
            elif code in (200, 201):  # GitHub accepted it but the answer has no URL we know: let gh post the files instead
                unreadable = sorted(answer)
                break
            else:
                errors.append({"criterion": criterion, "error": f"upload failed ({code}): {str(answer.get('message'))[:200]}".replace(token, "***")})
        if not (refused or unreadable):
            if errors:
                return _failed("upload", f"{len(errors)} of {len(videos)} videos did not upload", uploads=uploads, errors=errors, path="upload_endpoint")
            return {"status": "videos_uploaded", "run_id": run_id, "path": "upload_endpoint", "uploads": uploads, "deleted": _delete_videos(run_dir)}
    elif status not in _REFUSED:
        return _failed("repository", f"could not look up {repo} on GitHub ({status})", detail=str(repository.get("message"))[:200].replace(token, "***"))

    posted = _comment_with_gh(repo, number, body, list(videos.values()), token)  # the endpoint refused the token
    if posted.returncode != 0:
        return _failed("fallback", "the upload endpoint refused the token and gh could not post the comment", path="gh_pr_comment",
                       detail=_tail(posted, token))
    url = next((w for w in (posted.stdout or "").split() if w.startswith("https://github.com/")), None)
    extra = {"upload_response_keys": unreadable} if unreadable else {}  # what the endpoint answered with, for the next fix
    return {"status": "videos_uploaded", "run_id": run_id, "path": "gh_pr_comment", "comment_url": url,
            "criteria": sorted(videos), "deleted": _delete_videos(run_dir), **extra}


# --- Testing Agent: seed test data through the controllers --------------------------------------
# seed_docs inserts the documents the Testing Agent generates with frappe.get_doc(...).insert(), as Administrator, so they pass the
# same controller validation as real data. A factory such as make_employee is called only when it is on the allowed list. Nothing
# in a payload is ever evaluated: documents are plain data, and a factory is looked up by its dotted path only after that check.
# seed_context tells the agent what a DocType needs before it writes the data. Both run inside Frappe, like trigger_job.
SEED_FACTORIES_DEFAULT = ("one_fm.one_fm.tests.utils.make_employee",)
_FACTORY_DOCTYPES = {"one_fm.one_fm.tests.utils.make_employee": "Employee"}
SEED_MAX_DOCS = 50  # a fast command answers within 60 seconds, so large seed lists go in batches of this size
SEED_MAX_DOCTYPES = 10
SEED_TIMEOUT_SECONDS = 55
SEED_MAX_BYTES = 200_000
_DOCTYPE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _/-]{0,139}\Z")
_FACTORY_PATH = re.compile(r"\A[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,8}\Z")

# Data is data, but a document of one of these types would run code later (a script, a patch, a scheduled job).
_SEED_SCRIPT_COMMON = """
import json, os, frappe
payload = json.loads(os.environ["SEED_PAYLOAD"])
allowed = set(json.loads(os.environ["SEED_FACTORIES"]))
hints = json.loads(os.environ["SEED_FACTORY_HINTS"])
frappe.init(site=os.environ["SANDBOX_SITE"], sites_path=".")
frappe.connect()
"""

_SEED_DOCS_SCRIPT = _SEED_SCRIPT_COMMON + """
DENIED = {"Server Script", "Client Script", "Custom Script", "Patch Log", "Scheduled Job Type", "Console Log", "System Console"}
results = []
try:
    frappe.set_user("Administrator")
    for index, entry in enumerate(payload):
        point = "seed_%d" % index
        frappe.db.savepoint(point)
        try:
            if "factory" in entry:
                path = entry["factory"]
                if path not in allowed:
                    raise Exception("factory %s is not on the sandbox's allowed list of test factories" % path)
                made = frappe.get_attr(path)(**(entry.get("args") or {}))
                name = made if isinstance(made, str) else getattr(made, "name", None)
                results.append({"index": index, "factory": path, "name": name})
            else:
                if entry.get("doctype") in DENIED:
                    raise Exception("documents of type %s are not seeded: they would run code" % entry.get("doctype"))
                doc = frappe.get_doc(entry).insert()
                results.append({"index": index, "doctype": doc.doctype, "name": doc.name})
        except Exception as exc:
            frappe.db.rollback(save_point=point)
            results.append({"index": index, "doctype": entry.get("doctype"), "factory": entry.get("factory"), "error": str(exc)[:500]})
    frappe.db.commit()
finally:
    frappe.destroy()
print("SEED_JSON=" + json.dumps(results))
"""

_SEED_CONTEXT_SCRIPT = _SEED_SCRIPT_COMMON + """
import inspect
from frappe.test_runner import get_dependencies
LAYOUT = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Image", "Fold"}
order, seen, problems = [], set(), {}

def visit(doctype):
    if doctype in seen or len(seen) >= 60:
        return
    seen.add(doctype)
    try:
        for dependency in get_dependencies(doctype) or []:
            visit(dependency)
    except Exception as exc:
        problems[doctype] = str(exc)[:200]
    order.append(doctype)

def describe(doctype):
    meta = frappe.get_meta(doctype)
    fields = [f for f in meta.fields if f.fieldtype not in LAYOUT]
    try:
        validate = getattr(frappe.get_controller(doctype), "validate", None)
        source = inspect.getsource(validate)[:6000] if validate else ""
    except Exception:
        source = ""
    return {
        "required": [{"fieldname": f.fieldname, "fieldtype": f.fieldtype, "options": f.options} for f in fields if f.reqd],
        "links": [{"fieldname": f.fieldname, "target": f.options} for f in fields if f.fieldtype == "Link"],
        "tables": [{"fieldname": f.fieldname, "child": f.options} for f in fields if f.fieldtype in ("Table", "Table MultiSelect")],
        "selects": {f.fieldname: [o for o in (f.options or "").split("\\n") if o] for f in fields if f.fieldtype == "Select"},
        "mandatory_depends_on": {f.fieldname: f.mandatory_depends_on for f in fields if f.mandatory_depends_on},
        "validate": source,
        "factories": sorted(path for path in allowed if hints.get(path) == doctype),
    }

try:
    for name in payload:
        visit(name)
    out = {}
    for doctype in order:
        try:
            out[doctype] = describe(doctype)
        except Exception as exc:
            out[doctype] = {"error": str(exc)[:200]}
finally:
    frappe.destroy()
print("SEED_JSON=" + json.dumps({"order": order, "doctypes": out, "problems": problems, "factories": sorted(allowed)}))
"""


def _seed_factories():
    """The factories a seed list may call: the default list, or SEED_FACTORIES (a JSON list) where an operator sets it."""
    try:
        configured = json.loads(os.environ.get("SEED_FACTORIES", ""))
        if isinstance(configured, list) and all(isinstance(p, str) and _FACTORY_PATH.match(p) for p in configured):
            return set(configured)
    except ValueError:
        pass
    return set(SEED_FACTORIES_DEFAULT)


def _seed_run(script, payload):
    """Run a seed script in the bench's Python. Returns (parsed result, error)."""
    env = {**os.environ, "SANDBOX_SITE": SITE_NAME, "SEED_PAYLOAD": json.dumps(payload), "SEED_FACTORIES": json.dumps(sorted(_seed_factories())),
           "SEED_FACTORY_HINTS": json.dumps(_FACTORY_DOCTYPES)}
    run = _exec([f"{BENCH_DIR}/env/bin/python", "-c", script], env=env, cwd=f"{BENCH_DIR}/sites", timeout=SEED_TIMEOUT_SECONDS)
    if run.returncode == 124:
        return None, f"it did not finish within {SEED_TIMEOUT_SECONDS} seconds: send fewer documents per call"
    line = next((l for l in (run.stdout or "").splitlines() if l.startswith("SEED_JSON=")), None)
    if run.returncode != 0 or not line:
        return None, f"the sandbox site could not run it: {_tail(run)}"
    return json.loads(line[len("SEED_JSON="):]), None


def _seed_ready(args):
    """The run's marker when the head site is prepared and checked out, else an error answer."""
    run_id = args.get("run_id")
    error = _check_run_id(run_id)
    if error:
        return None, {"error": error}
    marker = _load_prepared(run_id)
    if not marker or "head_sha" not in marker:
        return None, {"error": _NOT_PREPARED}
    return marker, None


def seed_docs(target_app, args):
    """AC1-AC4: insert each document through its controller, in order, and report the name or the error for each. A document
    that fails does not stop the ones after it, and nothing in the payload is evaluated."""
    docs = args.get("docs")
    error = _check_app(target_app)
    if not error and (not isinstance(docs, list) or not docs or len(docs) > SEED_MAX_DOCS):
        error = f"docs must be a list of 1 to {SEED_MAX_DOCS} documents: split larger lists into batches."
    if not error:
        for i, entry in enumerate(docs):
            if not isinstance(entry, dict):
                error = f"docs[{i}] must be an object."
            elif ("factory" in entry) == ("doctype" in entry):
                error = f"docs[{i}] must have either a doctype (a document) or a factory (a test factory), not both or neither."
            elif "factory" in entry and (not isinstance(entry["factory"], str) or not _FACTORY_PATH.match(entry["factory"])
                                         or not isinstance(entry.get("args", {}), dict)):
                error = f"docs[{i}] names a factory that is not a dotted path, or its args are not an object."
            elif "doctype" in entry and (not isinstance(entry["doctype"], str) or not _DOCTYPE_NAME.match(entry["doctype"])):
                error = f"docs[{i}] has an invalid doctype name."
            if error:
                break
    if not error:
        try:
            if len(json.dumps(docs)) > SEED_MAX_BYTES:
                error = f"the documents are over {SEED_MAX_BYTES // 1000} KB: send fewer per call."
        except (TypeError, ValueError):
            error = "the documents must be plain JSON data."
    if error:
        return {"error": error}
    _marker, not_ready = _seed_ready(args)
    if not_ready:
        return not_ready
    results, error = _seed_run(_SEED_DOCS_SCRIPT, docs)
    if error:
        return {"error": f"seed_docs: {error}"}
    failed = [r for r in results if r.get("error")]
    return {"results": results, "inserted": len(results) - len(failed), "failed": len(failed)}


def seed_context(target_app, args):
    """AC5: what to know before seeding each DocType, dependencies first: required fields, Link targets, Select options,
    mandatory_depends_on, the controller's validate source and the factories that make it."""
    names = args.get("doctypes")
    error = _check_app(target_app)
    if not error and (not isinstance(names, list) or not names or len(names) > SEED_MAX_DOCTYPES
                      or not all(isinstance(n, str) and _DOCTYPE_NAME.match(n) for n in names)):
        error = f"doctypes must be a list of 1 to {SEED_MAX_DOCTYPES} DocType names."
    if error:
        return {"error": error}
    _marker, not_ready = _seed_ready(args)
    if not_ready:
        return not_ready
    context, error = _seed_run(_SEED_CONTEXT_SCRIPT, names)
    return {"error": f"seed_context: {error}"} if error else context


SPEC_TOOLS = {"write_spec": write_spec, "browser_snapshot": browser_snapshot, "trigger_job": trigger_job,
              "seed_docs": seed_docs, "seed_context": seed_context}


_TEST_FAILURE_LINE = re.compile(r"^(?:FAIL|ERROR): (.+)$", re.MULTILINE)
_NODE_FAILURE_LINE = re.compile(r"^\s*not ok \d+ - (.+)$", re.MULTILINE)


def _extract_failing_tests(*texts, limit=50):
    """Failing/erroring test identifiers from Python unittest's own output
    (what `bench run-tests` uses under the hood) -- the "FAIL: <id>" /
    "ERROR: <id>" summary lines its TextTestRunner always prints, regardless
    of which of stdout/stderr Frappe happened to send them to. Deliberately
    format-agnostic about the exact <id> shape (unittest's own formatting
    has changed across Python versions) -- whatever it printed is what an
    agent needs to search for, verbatim."""
    names = []
    seen = set()
    for text in texts:
        for match in (*_TEST_FAILURE_LINE.finditer(text or ""), *_NODE_FAILURE_LINE.finditer(text or "")):
            name = match.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
                if len(names) >= limit:
                    return names
    return names


def _run_tests(target_app, base_branch=None):
    # mobile_app_ionic isn't a Frappe app — no bench run-tests target exists
    # for it at all. Scope deliberately kept to build+unit-tests only:
    # yarn build (catches TS/template errors, matches CI) + vitest unit
    # tests. Cypress e2e is out of scope on purpose — it needs a running
    # app + backend to hit, which this sandbox has no business standing up
    # for a single dispatched change.
    if target_app == "mobile_app_ionic":
        app_dir = f"{BENCH_DIR}/apps/{target_app}"
        # node_modules baked into the image can predate package.json, and the repo's yarn.lock can lag it too, so a
        # plain install when the install state is stale. yarn.lock is put back so the install never reaches the PR.
        deps = _run(f"cd {shlex.quote(app_dir)} && (yarn check --integrity > /dev/null 2>&1 || yarn install) && git checkout -- yarn.lock", timeout=900)
        if deps.returncode != 0:
            return False, deps.stdout[-4000:], deps.stderr[-4000:], []
        build = _run(f"cd {shlex.quote(app_dir)} && yarn build", timeout=900)
        if build.returncode != 0:
            # A build failure, not a test failure -- vitest never ran, so
            # there is no unittest-style FAIL:/ERROR: summary to extract.
            return False, build.stdout[-4000:], build.stderr[-4000:], []
        # vitest watches by default and never exits, so run it once, and only the tests the change affects: a
        # failing test elsewhere in the repo must not decide the result.
        scope = ("--changed", f"origin/{base_branch}") if base_branch else ()
        test_args = " ".join(shlex.quote(arg) for arg in ("--run", "--passWithNoTests", *scope))
        test_command = f"cd {shlex.quote(app_dir)} && yarn test:unit {test_args}"
        test = _run(test_command, timeout=900)
        stdout = (build.stdout + test.stdout)[-4000:]
        stderr = (build.stderr + test.stderr)[-4000:]
        return test.returncode == 0, stdout, stderr, _extract_failing_tests(stdout, stderr)

    # agent_sandbox is this repository: a plain Python project with unittest tests at its top level and no Frappe site. bench run-tests
    # cannot run them, and the test-module matching below would find nothing and report a pass, so its suite is run as it is.
    if target_app in PYTHON_TEST_APPS:
        app_dir = f"{BENCH_DIR}/apps/{target_app}"
        run = _run(f"cd {shlex.quote(app_dir)} && python3 -m unittest discover -s . -p 'test_*.py'", timeout=900)
        return run.returncode == 0, run.stdout[-4000:], run.stderr[-4000:], _extract_failing_tests(run.stdout, run.stderr)

    # --skip-before-tests: erpnext's before_tests hook creates a default
    # Company on any fresh site, which cascades into Warehouse creation and
    # a genuine, pre-existing one_fm bug (before_insert_warehouse assumes a
    # Custom Field — one_fm_project — that only exists as live, un-exported
    # data, not a fixture) — crashing every test run on a fresh site
    # regardless of what the actual change touches. Confirmed live: a real
    # dispatch whose only change was adding a docstring to processa/utils.py
    # still failed here. This is a workaround for that unrelated fixture
    # gap, not a fix for it — one_fm's own bug is untouched and would still
    # break a real Company/Warehouse creation anywhere else it happens.
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    changed = _changed_paths(target_app, base_branch)
    modules = _test_modules_for(app_dir, changed)
    node_tests = _node_tests_for(app_dir, changed)
    if not modules and not node_tests:
        return True, "No test file matches the changed files, so no tests ran.", "", []
    passed, out, err = True, "", ""
    for module in modules[:_MAX_TEST_MODULES]:
        result = _run(
            f"cd {shlex.quote(BENCH_DIR)} && bench --site {shlex.quote(SITE_NAME)} run-tests --app {shlex.quote(target_app)} "
            f"--module {shlex.quote(module)} --skip-before-tests",
            timeout=1800,
        )
        passed = passed and result.returncode == 0
        out += result.stdout[-4000:]
        err += result.stderr[-4000:]
    if node_tests:
        frontend_dir = f"{app_dir}/frontend"
        node = ["node", "--test", *node_tests[:_MAX_TEST_MODULES]]
        result = _run(f"cd {shlex.quote(frontend_dir)} && " + shlex.join(node), timeout=300)
        passed = passed and result.returncode == 0
        out += result.stdout[-4000:]
        err += result.stderr[-4000:]
    stdout, stderr = out[-4000:], err[-4000:]
    failing = _extract_failing_tests(out, err)
    # bench run-tests exits 0 when a test raises an error, so a named failing test fails the run.
    return passed and not failing, stdout, stderr, failing


_MAX_TEST_MODULES = 10


def _test_modules_for(app_dir, paths):
    """Dotted test modules for the changed files: a changed test file itself, or the test file beside or above a changed source file."""
    modules = []
    for rel in paths:
        base, ext = os.path.splitext(rel)
        if ext not in (".py", ".json"):
            continue
        folder, stem = os.path.split(base)
        if ext == ".py" and stem.startswith("test_"):
            candidates = [rel]
        else:
            candidates = [
                f"{folder}/test_{stem}.py",
                f"{folder}/tests/test_{stem}.py",
                f"{os.path.dirname(folder)}/tests/test_{stem}.py",
            ]
        for candidate in candidates:
            if os.path.isfile(os.path.join(app_dir, candidate)):
                modules.append(candidate[:-3].replace("/", "."))
                break
    return list(dict.fromkeys(modules))


_NODE_TEST_SUFFIX = ".test.mjs"


def _node_tests_for(app_dir, paths):
    """The Vue app's node tests for the changed files: a changed test, the test beside a changed file, or a test that names it."""
    src = os.path.join(app_dir, "frontend", "src")
    changed = [path[len("frontend/src/") :] for path in paths if path.startswith("frontend/src/")]
    if not changed or not os.path.isdir(src):
        return []
    tests = {}
    for folder, _dirs, names in os.walk(src):
        for name in names:
            if name.endswith(_NODE_TEST_SUFFIX):
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as handle:
                    tests[os.path.relpath(path, src)] = handle.read()
    wanted = []
    for rel in changed:
        if rel in tests:
            wanted.append(rel)
            continue
        beside = os.path.splitext(rel)[0] + _NODE_TEST_SUFFIX
        wanted.extend([beside] if beside in tests else [test for test, text in tests.items() if rel in text])
    return ["src/" + test for test in dict.fromkeys(wanted)]


_test_results = {}
_TEST_RESULTS_KEPT = 20


def _head_commit(target_app):
    """The commit the working tree is on, so a test result can be tied to the code it ran against."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    return _run(f"cd {shlex.quote(app_dir)} && git rev-parse HEAD").stdout.strip()


def _tree_fingerprint(target_app):
    """The commit plus everything not yet committed, so equal fingerprints mean an identical tree."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    head = _run(f"cd {shlex.quote(app_dir)} && git rev-parse HEAD").stdout.strip()
    pending = _run(f"cd {shlex.quote(app_dir)} && git status --porcelain && git diff HEAD").stdout
    return f"{head}:{hashlib.sha1(pending.encode()).hexdigest()}"


def _tests_for_tree(target_app, base_branch=None):
    """_run_tests for the tree as it stands, reusing the last result while the tree has not changed."""
    key = (target_app, _tree_fingerprint(target_app))
    if key in _test_results:
        return _test_results[key]
    result = _run_tests(target_app, base_branch)
    if len(_test_results) >= _TEST_RESULTS_KEPT:
        _test_results.pop(next(iter(_test_results)))
    _test_results[key] = result
    return result


def _collect_changed_files(target_app, base_branch=None):
    """{repo-relative path: final content, or None for a deleted file} for every
    file changed on the working branch: committed there by the fast tools
    (diffed against base_branch) or still uncommitted from the bundled coding loop."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    files = {}
    for rel_path in _changed_paths(target_app, base_branch):
        abs_path = f"{app_dir}/{rel_path}"
        if not os.path.lexists(abs_path):
            files[rel_path] = None
            continue
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
    result = _run(f"cd {shlex.quote(app_dir)} && git remote get-url origin")
    if result.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", result.stdout.strip())
    return match.group(1) if match else None


_GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?\Z")


def installed_apps():
    """Every app this sandbox holds: {"app": its folder in apps/, "repository": "owner/name"}. Processa is told this when a session
    registers, so the apps it accepts follow the image and nobody keeps a second list by hand. A folder that is not a git clone, or whose
    name is not a safe app name, is left out."""
    root = f"{BENCH_DIR}/apps"
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(root, name)
        if not _SAFE_APP.match(name) or not os.path.isdir(os.path.join(path, ".git")):
            continue
        remote = _exec(["git", "-C", path, "remote", "get-url", "origin"], timeout=15)
        match = _GITHUB_REMOTE.search((remote.stdout or "").strip()) if remote.returncode == 0 else None
        found.append({"app": name, "repository": match.group(1) if match else ""})
    return found


def app_dir_name(target_app):
    """The folder in apps/ for a target app that may be given as a repository name: a Work Item names the GitHub repository
    (password_management, agent-sandbox), while the folder can differ (one_fm_password_management, agent_sandbox). The name is
    returned unchanged when nothing matches, so the usual "not found" errors still say what was asked for."""
    name = str(target_app or "")
    if _SAFE_APP.match(name) and os.path.isdir(f"{BENCH_DIR}/apps/{name}"):
        return name
    for entry in installed_apps():
        repo_name = entry["repository"].split("/")[-1]
        if name and (repo_name == name or entry["app"] == name.replace("-", "_")):
            return entry["app"]
    return name


def _open_pr(
    target_app, git_branch, work_item_description, files, github_token, correlation_id, agent_report,
    tests_passed=True, stderr_tail="", work_item_id=None, agent_name="Dev Agent",
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

    # The same branch the fast tools committed to, so the PR carries their
    # work; keyed to the work order rather than correlation_id so a retry
    # updates one PR instead of opening another.
    head_branch = _head_branch_for(target_app, git_branch, work_item_description, work_item_id)
    work_item_id = (work_item_id or "").strip()
    title_prefix = "" if tests_passed else "⚠️ Tests failed: "
    subject = f"{work_item_id}: {work_item_description[:72]}" if work_item_id else f"{agent_name}: {work_item_description[:72]}"
    title = f"{title_prefix}{subject}"
    file_list = "\n".join(f"- `{path}`" + (" (deleted)" if files[path] is None else "") for path in sorted(files))
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
    work_item_line = f"Work Item: **{work_item_id}**\n\n" if work_item_id else ""
    body = (
        f"Opened automatically by the agent sandbox ({correlation_id}).\n\n"
        f"{work_item_line}"
        f"## Work order\n\n{work_item_description}\n\n"
        f"## What changed\n\n{agent_report.strip() or '(the agent finished without a summary)'}\n\n"
        f"## Files changed\n\n{file_list}\n\n"
        f"## Testing\n\n{testing_section}"
    )

    # Changes the fast tools or a session checkpoint already pushed are on the branch. Only what is still
    # uncommitted here (the bundled coding loop) goes up through the Contents API.
    pending = _pending_paths(target_app)
    uploads = {path: content for path, content in files.items() if path in pending}
    try:
        if uploads:
            ref = _github_request("GET", f"{_GITHUB_API}/repos/{repo}/git/ref/heads/{git_branch}", github_token)
            base_sha = ref["object"]["sha"]
            _github_request(
                "POST",
                f"{_GITHUB_API}/repos/{repo}/git/refs",
                github_token,
                ok=(201, 422),  # 422: branch already exists, reuse it
                json_body={"ref": f"refs/heads/{head_branch}", "sha": base_sha},
            )

        for path, content in uploads.items():
            existing = _github_request(
                "GET",
                f"{_GITHUB_API}/repos/{repo}/contents/{path}?ref={head_branch}",
                github_token,
                ok=(200, 404),
            )
            if content is None:
                if existing and existing.get("sha"):
                    _github_request(
                        "DELETE", f"{_GITHUB_API}/repos/{repo}/contents/{path}", github_token,
                        json_body={"message": title, "sha": existing["sha"], "branch": head_branch},
                    )
                continue
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
            lines = fh.readlines()
    except FileNotFoundError:
        return {"found": False, "content": ""}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": str(exc)}

    offset, limit = args.get("offset"), args.get("limit")
    if offset is None and limit is None:
        return {"found": True, "content": "".join(lines)}

    # 1-indexed, inclusive — matches how line numbers get cited in work
    # orders (e.g. "observability.py:620-653"). Confirmed live: without
    # this, a model working from cited line numbers re-read a large file
    # whole a dozen-plus times trying to reconcile them, burning most of
    # its turn budget before ever writing anything.
    start = max(0, (offset or 1) - 1)
    end = start + limit if limit else len(lines)
    return {
        "found": True,
        "content": "".join(lines[start:end]),
        "total_lines": len(lines),
        "returned_lines": f"{start + 1}-{min(end, len(lines))}",
    }


def _tool_write_file(app_dir, args):
    path = args.get("path") or ""
    content = args.get("content")
    if content is None:
        return {"error": "content is required"}
    try:
        abs_path = _safe_path(app_dir, path)
    except ValueError as exc:
        return {"error": str(exc)}
    if not content.strip() and os.path.isfile(abs_path) and os.path.getsize(abs_path) > 0:
        return {
            "error": f"This would empty {path!r}. To remove a file that is no longer needed use delete_file; "
            "if it must stay as an empty file, delete it first and then write it empty."
        }
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return {"written": True, "path": path}


def _tool_delete_file(app_dir, args):
    """Remove one file. Only a file: a directory, or a path that does not exist, is reported back as an error."""
    path = args.get("path") or ""
    # The same check as _safe_path, written inline so the scanner sees the guard on this remove().
    abs_path = os.path.realpath(os.path.join(app_dir, path))
    if not abs_path.startswith(app_dir + os.sep):
        return {"error": f"path escapes the app directory: {path!r}"}
    if not os.path.isfile(abs_path):
        return {"error": f"{path!r} is not a file in this app"}
    os.remove(abs_path)
    return {"deleted": True, "path": path}


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
        return {
            "error": "old_string not found in the file — it may have changed since you last read it",
            **_closest_text(content, old_string),
        }
    if count > 1:
        return {"error": f"old_string appears {count} times — include more surrounding context to make it unique"}
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content.replace(old_string, new_string, 1))
    return {"edited": True, "path": path}


def _closest_text(content, old_string):
    """The file's current lines around the line most like old_string's first
    line, so the next edit can be written against what is there now."""
    lines = content.splitlines(keepends=True)
    stripped = [line.strip() for line in lines]
    wanted = next((line.strip() for line in old_string.splitlines() if line.strip()), "")
    match = difflib.get_close_matches(wanted, stripped, n=1, cutoff=0.6)
    if not match:
        return {}
    index = stripped.index(match[0])
    start = max(0, index - 3)
    end = min(len(lines), index + len(old_string.splitlines()) + 3)
    return {"current_text": "".join(lines[start:end]), "current_lines": f"{start + 1}-{end}"}


_READ_FILES_MAX = 10
_READ_FILES_MAX_CHARS = 20_000
_GREP_MAX_MATCHES = 100
_GREP_LINE_CHARS = 300
_APPLY_EDITS_MAX = 20


def _tool_read_files(app_dir, args):
    """Several files in one call: each as read_file returns it, cut at a size that fits one tool result."""
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return {"error": "paths must be a non-empty list of file paths"}
    if len(paths) > _READ_FILES_MAX:
        return {"error": f"at most {_READ_FILES_MAX} paths per call, got {len(paths)}"}
    files = []
    for path in paths:
        one = _tool_read_file(app_dir, {"path": path})
        content = one.get("content")
        if isinstance(content, str) and len(content) > _READ_FILES_MAX_CHARS:
            one = {**one, "content": content[:_READ_FILES_MAX_CHARS], "truncated": True,
                   "total_chars": len(content), "hint": "Read the rest with read_file and offset and limit."}
        files.append({"path": path, **one})
    return {"files": files}


def _tool_grep_files(app_dir, args):
    """Lines matching a pattern across the app (tracked and new files, binaries skipped), capped in count and width."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return {"error": "pattern is required"}
    prefix = (args.get("path_prefix") or "").strip().lstrip("/")
    try:
        scope = os.path.relpath(_safe_path(app_dir, prefix), app_dir)
    except ValueError as exc:
        return {"error": str(exc)}
    mode = "--fixed-strings" if args.get("literal") else "--extended-regexp"
    found = _run(
        f"cd {shlex.quote(app_dir)} && git grep --untracked -n -I --no-color {mode} -e {shlex.quote(pattern)} -- {shlex.quote(scope)}"
    )
    if found.returncode not in (0, 1):
        return {"error": found.stderr[-500:] or "grep failed"}
    lines = found.stdout.splitlines()
    matches = []
    for line in lines[:_GREP_MAX_MATCHES]:
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        matches.append({"path": path, "line": int(number) if number.isdigit() else None, "text": text[:_GREP_LINE_CHARS]})
    return {"matches": matches, "count": len(matches), "truncated": len(lines) > _GREP_MAX_MATCHES}


def _tool_apply_edits(app_dir, args):
    """Several edit_file replacements in order. Stops at the first one that fails; earlier ones stay applied."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits or not all(isinstance(e, dict) for e in edits):
        return {"error": "edits must be a non-empty list of {path, old_string, new_string}"}
    if len(edits) > _APPLY_EDITS_MAX:
        return {"error": f"at most {_APPLY_EDITS_MAX} edits per call, got {len(edits)}"}
    paths = []
    for index, edit in enumerate(edits):
        one = _tool_edit_file(app_dir, edit)
        if not one.get("edited"):
            return {"edited": False, "applied": index, "paths": paths, "failed_edit": index, **one}
        paths.append(one["path"])
    return {"edited": True, "applied": len(edits), "paths": list(dict.fromkeys(paths))}


# Confirmed live: processa alone (one target app, not the whole bench) already
# has 738 files under its own tree — a 500 cap silently truncated a real
# listing, and the model never acted on the "truncated": true it was given,
# spent its whole turn budget guessing narrower path_prefix values for a
# file that (separately) did not exist, and never finished. Raised well
# past any single target app's realistic size so truncation stops being
# the common case; still bounded so a pathological repo can't return an
# unbounded response.
_LIST_FILES_MAX = 5000

# Confirmed live (2026-09-07): an unscoped call on a real app (no narrow
# path_prefix) hit this file-count cap and returned a ~277KB listing —
# every turn afterward re-sent that same blob as part of the transcript,
# so one list_files call alone accounted for the bulk of a run's token
# usage. The count cap bounds pathological depth; this bounds the size of
# what a single ordinary call can cost, independent of how many files that
# turns out to be.
_LIST_FILES_MAX_CHARS = 50_000
_LIST_FILES_SKIPPED_DIRS = {".git", "node_modules", "__pycache__", "dist"}


def _tool_list_files(app_dir, args):
    prefix = (args.get("path_prefix") or "").strip().lstrip("/")
    try:
        start_dir = _safe_path(app_dir, prefix) if prefix else app_dir
    except ValueError as exc:
        return {"error": str(exc)}
    paths = []
    line_counts = {}
    chars = 0
    for root, dirs, filenames in os.walk(start_dir):
        dirs[:] = [d for d in dirs if d not in _LIST_FILES_SKIPPED_DIRS]
        for name in filenames:
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, app_dir)
            paths.append(rel)
            # Counting reads every file, so only the directory the model asked about is counted.
            if prefix:
                line_counts[rel] = _line_count(app_dir, rel)
            chars += len(rel) + len(str(line_counts.get(rel, ""))) + 6  # quotes, colon, comma, close enough to bound intent
            if len(paths) >= _LIST_FILES_MAX or chars >= _LIST_FILES_MAX_CHARS:
                return {"files": paths, "line_counts": line_counts, "count": len(paths), "truncated": True}
    return {"files": paths, "line_counts": line_counts, "count": len(paths), "truncated": False}


def _line_count(app_dir, rel_path):
    """Lines as read_file counts them (newlines plus one), so a model can plan offset and limit.

    None for a binary file, one that cannot be opened, or a symlink that leads outside the app directory.
    """
    # The same check as _safe_path, written inline so the scanner sees the guard on this open().
    abs_path = os.path.realpath(os.path.join(app_dir, rel_path))
    if not abs_path.startswith(app_dir + os.sep):
        return None
    try:
        with open(abs_path, "rb") as fh:
            data = fh.read()
    except OSError:  # a broken symlink, or a file the server cannot read
        return None
    if b"\0" in data[:1024]:
        return None
    return data.count(b"\n") + 1


def _tool_run_tests(target_app, args, base_branch=None):
    passed, stdout, stderr, failing_tests = _tests_for_tree(target_app, base_branch)
    return {
        "passed": passed, "stdout_tail": stdout[-2000:], "stderr_tail": stderr[-2000:],
        "failing_tests": failing_tests,
    }


def _tool_open_pull_request(target_app, args, run_ctx):
    """PR-creation as something the model explicitly decides to call, rather
    than automatic post-loop logic (run_job no longer calls _open_pr itself —
    see its own docstring). Takes the test result for the tree as it stands
    now: reused when the tree is unchanged since the model's last run_tests,
    run fresh when anything changed after it, so the PR's pass/fail flagging
    never depends on the model remembering to re-test."""
    summary = (args.get("summary") or "").strip()
    files = _collect_changed_files(target_app, run_ctx["git_branch"])
    if not files:
        return {"error": f"The working branch has no changes against {run_ctx['git_branch']}. Make an edit first, then open the pull request."}
    passed, _stdout, stderr, failing_tests = _tests_for_tree(target_app, run_ctx["git_branch"])
    pr_url, pr_error = _open_pr(
        target_app, run_ctx["git_branch"], run_ctx["work_item_description"], files,
        run_ctx["github_token"], run_ctx["correlation_id"], summary,
        tests_passed=passed, stderr_tail=stderr, work_item_id=run_ctx.get("work_item_id"),
        agent_name=run_ctx.get("agent_name") or "Dev Agent",
    )
    if pr_url:
        return {"pr_url": pr_url, "tests_passed": passed, "failing_tests": failing_tests}
    return {"error": pr_error, "tests_passed": passed, "failing_tests": failing_tests}


def _dispatch_tool(app_dir, target_app, name, args, run_ctx):
    if name == "read_file":
        return _tool_read_file(app_dir, args)
    if name == "write_file":
        return _tool_write_file(app_dir, args)
    if name == "edit_file":
        return _tool_edit_file(app_dir, args)
    if name == "delete_file":
        return _tool_delete_file(app_dir, args)
    if name == "list_files":
        return _tool_list_files(app_dir, args)
    if name == "run_tests":
        return _tool_run_tests(target_app, args, run_ctx["git_branch"])
    if name == "open_pull_request":
        return _tool_open_pull_request(target_app, args, run_ctx)
    # _validate_payload already rejects a dispatch naming an unimplemented
    # tool before the loop ever starts (see _KNOWN_TOOL_NAMES) — reaching
    # this means _KNOWN_TOOL_NAMES and this dispatcher drifted apart, not a
    # normal runtime outcome. Reported to the model like any other tool
    # error rather than crashing the loop.
    return {"error": f"unknown tool {name!r} — the sandbox has no implementation for it"}


def _handle_tool_call(payload, timings=None):
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
    work_item_id = payload.get("work_item_id")
    args = payload.get("args") or {}
    agent_name = (payload.get("agent_name") or "Dev Agent").strip() or "Dev Agent"
    timings = timings or _Timings()

    head_branch = _head_branch_for(target_app, git_branch, work_item_description, work_item_id)
    with timings.span("git"):
        ok, err = _checkout_or_create_head_branch(target_app, git_branch, head_branch)
    if not ok:
        return {"error": err}

    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    with timings.span("tool"):
        result, changed = _apply_tool(app_dir, target_app, action, args)
    if changed:
        with timings.span("git"):
            ok, err = _commit_and_push(target_app, head_branch, changed, f"{action} via {agent_name}", agent_name)
        if not ok:
            result["commit_error"] = err
    return result


# The file operations a fast action names: the result, and the path or paths it changed (None when it changed nothing).
_CHANGING_RESULT_KEYS = {"write_file": "written", "edit_file": "edited", "delete_file": "deleted", "apply_edits": "applied"}


def _apply_tool(app_dir, target_app, action, args):
    # search_frontend reports bench paths ("processa/frontend/..."); the app's own package keeps its "processa/..." paths.
    prefix = f"{target_app}/"
    for key in ("path", "path_prefix"):
        value = args.get(key)
        if isinstance(value, str) and value.startswith(prefix):
            top = value[len(prefix):].split("/", 1)[0]
            if top and not os.path.exists(os.path.join(app_dir, target_app, top)) and os.path.exists(os.path.join(app_dir, top)):
                args = {**args, key: value[len(prefix):]}
    tools = {
        "read_file": _tool_read_file, "list_files": _tool_list_files, "read_files": _tool_read_files,
        "grep_files": _tool_grep_files, "write_file": _tool_write_file, "edit_file": _tool_edit_file,
        "delete_file": _tool_delete_file, "apply_edits": _tool_apply_edits,
    }
    if action not in tools:
        # _validate_tool_call_payload already restricts action to _FAST_ACTIONS,
        # reaching this means that set and this dispatcher drifted apart.
        return {"error": f"unknown fast tool action {action!r}"}, None
    result = tools[action](app_dir, args)
    if not result.get(_CHANGING_RESULT_KEYS.get(action, "")):
        return result, None
    return result, result["paths"] if action == "apply_edits" else (args.get("path") or "")


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
    work_item_id = payload.get("work_item_id")
    github_token = payload["github_token"]
    args = payload.get("args") or {}
    callback_url = payload["callback_url"]
    timings = _Timings()

    head_branch = _head_branch_for(target_app, git_branch, work_item_description, work_item_id)

    _set_status(correlation_id, state="checking_out_branch")
    with timings.span("git"):
        ok, err = _checkout_or_create_head_branch(target_app, git_branch, head_branch)
    if not ok:
        _set_status(correlation_id, state="failed", error=err)
        _post_callback(
            callback_url,
            {"correlation_id": correlation_id, "status": "failed", "error": err, "timings_ms": timings.report()},
        )
        return

    _set_status(correlation_id, state="migrating")
    ok, mig_out, mig_err = True, "", ""
    if _needs_migrate(_changed_paths(target_app, git_branch)):
        with timings.span("migrate"):
            ok, mig_out, mig_err = _migrate_site()
    if not ok:
        _set_status(correlation_id, state="failed", error=mig_err)
        _post_callback(
            callback_url,
            {
                "correlation_id": correlation_id, "status": "failed", "error": mig_err,
                "stdout_tail": mig_out, "timings_ms": timings.report(),
            },
        )
        return

    if action == "run_tests":
        _set_status(correlation_id, state="running_tests")
        with timings.span("tests"):
            passed, stdout, stderr, failing_tests = _tests_for_tree(target_app, git_branch)
        body = {
            "correlation_id": correlation_id,
            "status": "tests_passed" if passed else "tests_failed",
            "stdout_tail": stdout,
            "stderr_tail": stderr,
            "failing_tests": failing_tests,
            "commit": _head_commit(target_app),
        }
    elif action == "open_pull_request":
        run_ctx = {
            "git_branch": git_branch,
            "work_item_description": work_item_description,
            "work_item_id": work_item_id,
            "github_token": github_token,
            "correlation_id": correlation_id,
            "agent_name": payload.get("agent_name"),
        }
        _set_status(correlation_id, state="opening_pr")
        with timings.span("pr"):
            result = _tool_open_pull_request(target_app, args, run_ctx)
        status = "tests_passed" if result.get("pr_url") else "tests_failed"
        body = {"correlation_id": correlation_id, "status": status}
        if result.get("pr_url"):
            body["pr_url"] = result["pr_url"]
        if result.get("error"):
            body["pr_error"] = result["error"]
        if result.get("failing_tests"):
            body["failing_tests"] = result["failing_tests"]
    else:
        # _validate_payload already restricts action to _SLOW_ACTIONS for
        # /run — reaching this means that set and this branch drifted apart.
        body = {"correlation_id": correlation_id, "status": "failed", "error": f"unknown action {action!r}"}

    body["timings_ms"] = timings.report()
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
    latency fields — matching the shape processa's own step-driven loop
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
        "work_item_id": payload.get("work_item_id"),
        "github_token": github_token,
        "correlation_id": correlation_id,
        "agent_name": payload.get("agent_name"),
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
    passed, stdout, stderr, failing_tests = _tests_for_tree(target_app, git_branch)

    status = "tests_passed" if passed else "tests_failed"
    body = {
        "correlation_id": correlation_id,
        "status": status,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "failing_tests": failing_tests,
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
    def _send_json(self, code, body, timings=None):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if timings:
            self.send_header("X-Timings-Ms", json.dumps(timings.report()))
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
        with _jobs_lock:
            known = correlation_id in _jobs
        if known:
            # A retried dispatch whose first answer was lost: the run is already going, so do not start it twice.
            self._send_json(202, {"correlation_id": correlation_id, "status": "accepted"})
            return
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

        timings = _Timings()
        try:
            result = _handle_tool_call(payload, timings)
        except Exception as exc:  # noqa: BLE001 — reported to the caller, not a 500
            self._send_json(200, {"error": f"tool_call failed: {exc}"}, timings)
            return
        self._send_json(200, result, timings)

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
