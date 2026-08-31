"""dev_agent_server.py

Generic, config-free driver for the dev-agent sandbox container.

This process IS the coding agent. It carries no system prompt, no model
choice, no tool list, and no credential of its own — every one of those
arrives fresh in the POST /run body's "agent_config" (system_prompt, model,
api_key), resolved by Processa from the "Dev Agent" AI Agent Configuration
and its linked AI Provider on every dispatch. This process never invents
behavior or a credential the caller didn't supply.

Given a work order (target_app, git_branch, work_item_description) and that
agent_config, it runs its own bounded tool-calling loop against the model it
was told to use — read_file / write_file / list_files against the checked-out
working tree, plus run_tests — deciding what to read, what to change, and
when it's done. Processa never sees the file plan in advance; it only gets
the sandbox's own final report and the actual diff, in the callback.

Stdlib plus the anthropic SDK (installed at image-build time — see
Dockerfile.frappe_runtime; dev_agent_server.py runs via system python3, not
the bench's own venv, per entrypoint.sh).

On a pass, this process delivers the change itself: it opens the pull
request directly against GitHub's REST API (see _open_pr below) — the same
Contents-API mechanism api/github_sync.py's open_customization_pr() already
uses on the Processa side for other PR-opening flows, just run from here
instead. Processa's callback handler only ever records the result (the PR
URL, or why one wasn't opened); it never talks to GitHub for this itself.
The GitHub token arrives fresh per dispatch in the request body, exactly
like agent_config's model credential — never a static secret baked into
this container's own deployment.
"""

import base64
import hashlib
import hmac
import json
import os
import re
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
    "github_token",
    "callback_url",
)
REQUIRED_AGENT_CONFIG_FIELDS = ("system_prompt", "model", "api_key")

# In-memory job status, keyed by correlation_id. Fine for one-instance-per-run
# (Cloud Run --concurrency=1) — no shared state needed across instances.
_jobs = {}
_jobs_lock = threading.Lock()


def _set_status(correlation_id, **fields):
    with _jobs_lock:
        job = _jobs.setdefault(correlation_id, {})
        job.update(fields)
        job["updated_at"] = time.time()


def _validate_payload(payload):
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"

    agent_config = payload["agent_config"]
    if not isinstance(agent_config, dict):
        return "agent_config must be an object"
    missing_ac = [f for f in REQUIRED_AGENT_CONFIG_FIELDS if not (agent_config.get(f) or "").strip()]
    if missing_ac:
        return f"agent_config missing required non-empty field(s): {', '.join(missing_ac)}"

    if not (payload.get("github_token") or "").strip():
        return "github_token must be a non-empty string"

    callback_url = payload["callback_url"]
    parsed = urllib.parse.urlparse(callback_url)
    if parsed.scheme != "https":
        return "callback_url must be https"
    if ALLOWED_CALLBACK_HOST and parsed.hostname != ALLOWED_CALLBACK_HOST:
        return f"callback_url host must be {ALLOWED_CALLBACK_HOST}"

    return None


def _run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)


def _checkout_target_branch(target_app, git_branch):
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    fetch = _run(f"cd {app_dir} && git fetch origin {git_branch}")
    if fetch.returncode != 0:
        return False, f"git fetch failed: {fetch.stderr}"
    checkout = _run(
        f"cd {app_dir} && git checkout {git_branch} || git checkout -b {git_branch} origin/{git_branch}"
    )
    if checkout.returncode != 0:
        return False, f"git checkout failed: {checkout.stderr}"
    return True, None


def _migrate_site():
    # Cheap once the site is baked (mostly a no-op) — necessary because the
    # branch just checked out for the target app can carry schema changes
    # the baked snapshot doesn't have yet.
    result = _run(f"cd {BENCH_DIR} && bench --site {SITE_NAME} migrate --skip-failing", timeout=600)
    return result.returncode == 0, result.stdout[-2000:], result.stderr[-2000:]


def _run_tests(target_app):
    result = _run(
        f"cd {BENCH_DIR} && bench --site {SITE_NAME} run-tests --app {target_app}",
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


def _open_pr(target_app, git_branch, work_item_description, files, github_token, correlation_id):
    """Create a branch off git_branch, commit every changed file via the
    Contents API, and open a PR. Returns (pr_url, None) on success or
    (None, error_message) on failure — never raises, so a PR-delivery
    problem is reported back like any other outcome, not a crash."""
    repo = _repo_for_local_clone(target_app)
    if not repo:
        return None, f"Could not determine the GitHub repository for {target_app!r} from its local clone."

    head_branch = f"dev-agent/{correlation_id.lower()}"
    title = f"Dev Agent: {work_item_description[:72]}"
    body = (
        f"Opened automatically by the Dev Agent sandbox ({correlation_id}).\n\n"
        f"Work order:\n\n{work_item_description}\n\n"
        "The target app's real test suite passed in an isolated, disposable "
        "sandbox before this PR was opened. Review as you would any other PR."
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
            json_body={"title": title, "head": head_branch, "base": git_branch, "body": body},
        )
        return pr.get("html_url", ""), None
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


_TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read one file's current content from the target app's working tree.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative file path."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a file's COMPLETE content into the target app's working tree, creating it if "
            "it does not exist. Overwrites the whole file — always include every line, not a diff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "content": {"type": "string", "description": "The file's full new content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "List file paths in the target app's working tree, optionally scoped to a prefix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path_prefix": {
                    "type": "string",
                    "description": "Optional repo-relative prefix to narrow the listing.",
                }
            },
        },
    },
    {
        "name": "run_tests",
        "description": (
            "Run the target app's real test suite against the working tree as it currently stands. "
            "Use this to check your own work before you finish — a final run happens after you stop "
            "regardless, but that one only decides pass/fail, it doesn't help you fix anything."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _dispatch_tool(app_dir, target_app, name, args):
    if name == "read_file":
        return _tool_read_file(app_dir, args)
    if name == "write_file":
        return _tool_write_file(app_dir, args)
    if name == "list_files":
        return _tool_list_files(app_dir, args)
    if name == "run_tests":
        return _tool_run_tests(target_app, args)
    return {"error": f"unknown tool {name!r}"}


def _run_coding_loop(target_app, work_item_description, agent_config):
    """The agent's own bounded tool-calling loop. Returns (final_text,
    iterations_used, hit_limit). Every decision here is the model's — this
    function only wires its tool calls to the filesystem and feeds results
    back, exactly as a human would relay a tool's output."""
    app_dir = f"{BENCH_DIR}/apps/{target_app}"
    client = anthropic.Anthropic(api_key=agent_config["api_key"])
    model = agent_config["model"]
    system_prompt = agent_config["system_prompt"]

    messages = [{"role": "user", "content": work_item_description}]

    for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
        response = client.messages.create(
            model=model,
            system=system_prompt,
            messages=messages,
            tools=_TOOL_SCHEMAS,
            max_tokens=AGENT_MAX_TOKENS,
        )

        if response.stop_reason != "tool_use":
            final_text = "".join(block.text for block in response.content if block.type == "text")
            return final_text, iteration, False

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _dispatch_tool(app_dir, target_app, block.name, block.input or {})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return (
        f"Stopped after {MAX_AGENT_ITERATIONS} tool-calling turns without finishing — "
        "reporting whatever state the working tree is currently in.",
        MAX_AGENT_ITERATIONS,
        True,
    )


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


def run_job(payload):
    correlation_id = payload["correlation_id"]
    target_app = payload["target_app"]
    git_branch = payload["git_branch"]
    work_item_description = payload["work_item_description"]
    agent_config = payload["agent_config"]
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
    try:
        agent_report, iterations, hit_limit = _run_coding_loop(target_app, work_item_description, agent_config)
    except Exception as exc:  # noqa: BLE001 — reported to the caller, not raised here
        err = f"coding loop failed: {exc}"
        _set_status(correlation_id, state="failed", error=err)
        _post_callback(callback_url, {"correlation_id": correlation_id, "status": "failed", "error": err})
        return

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
    }
    if passed:
        # Only ever open a PR on a genuine pass — a failing change must
        # never reach GitHub at all.
        files = _collect_changed_files(target_app)
        if not files:
            status = body["status"] = "tests_passed_no_changes"
        else:
            body["files"] = files
            _set_status(correlation_id, state="opening_pr")
            pr_url, pr_error = _open_pr(target_app, git_branch, work_item_description, files, github_token, correlation_id)
            if pr_url:
                body["pr_url"] = pr_url
            else:
                body["pr_error"] = pr_error

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

    def do_POST(self):
        if self.path != "/run":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        error = _validate_payload(payload)
        if error:
            self._send_json(422, {"error": error})
            return

        correlation_id = payload.get("correlation_id") or str(uuid.uuid4())
        _set_status(correlation_id, state="accepted")
        threading.Thread(target=run_job, args=(payload,), daemon=True).start()
        self._send_json(202, {"correlation_id": correlation_id, "status": "accepted"})

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
