"""Sandbox session agent.

Runs in a Cloud Run Job container, one per Work Item attempt. It never listens for requests: it
registers with Processa using a one-time token, then polls for numbered commands, carries each one
out on its own checkout of the Work Item's branch, and posts the result signed with the session key.
Processa decides everything; this only holds the workspace.

Environment: SESSION_ID, SESSION_TOKEN, PROCESSA_URL. ALLOWED_PROCESSA_HOST pins the host it may call.
"""

import hashlib
import hmac
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import dev_agent_server as srv

API = "/api/method/one_bpmn.api.sandbox_session"
POLL_MIN_SECONDS = 0.3
POLL_MAX_SECONDS = 2.0
CHECKPOINT_SECONDS = 300
# The agent idles this long past Processa's idle timeout, so Processa's close command arrives before the agent exits.
IDLE_GRACE_SECONDS = 180
# About 90 seconds of retrying, so a Processa deploy or a dropped tunnel does not end a live session.
CALL_TRIES = 10
CALL_BACKOFF_MAX_SECONDS = 15
# A tunnel that is briefly offline answers 404; 408, 425 and 429 are also worth waiting out.
RETRY_CLIENT_CODES = frozenset({404, 408, 425, 429})


class SessionRefused(Exception):
    """Processa no longer accepts this session: it was closed, lost or never registered."""


class Processa:
    def __init__(self, base_url, session_id):
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.key = ""

    def call(self, endpoint, body):
        raw = json.dumps({"session": self.session_id, "ts": time.time(), **body}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-Signature"] = hmac.new(self.key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        request = urllib.request.Request(f"{self.base_url}{API}.{endpoint}", data=raw, headers=headers, method="POST")
        last = None
        for attempt in range(CALL_TRIES):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = json.loads(response.read() or b"{}")
                return data.get("message", data)
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code not in RETRY_CLIENT_CODES:
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
            time.sleep(min(0.5 * 2**attempt, CALL_BACKOFF_MAX_SECONDS))
        raise last


def check_processa_url(url):
    """The address must be https (http only for a local run) and on one of the pinned hosts when any are set."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and srv.SANDBOX_ENV != "local":
        raise ValueError("PROCESSA_URL must be https")
    pinned = [host.strip() for host in os.environ.get("ALLOWED_PROCESSA_HOST", "").split(",") if host.strip()]
    if pinned and parsed.hostname not in pinned:
        raise ValueError(f"PROCESSA_URL host must be one of {', '.join(pinned)}")


class Session:
    def __init__(self, client, settings):
        self.client = client
        self.target_app = settings["target_app"]
        self.git_branch = settings["git_branch"]
        self.work_item = settings["work_item"]
        self.agent_name = (settings.get("agent_name") or "Dev Agent").strip() or "Dev Agent"
        self.github_token = settings.get("github_token") or ""
        self.idle_timeout = settings.get("idle_timeout_seconds", 600) + IDLE_GRACE_SECONDS
        self.app_dir = f"{srv.BENCH_DIR}/apps/{self.target_app}"
        self.head_branch = srv._head_branch_for(self.target_app, self.git_branch, "", self.work_item)
        self.unpushed = False
        self.last_push = time.monotonic()

    def prepare(self):
        """Check the branch out once. Every later command works on this checkout."""
        os.environ["GITHUB_TOKEN"] = self.github_token
        ok, err = srv._checkout_or_create_head_branch(self.target_app, self.git_branch, self.head_branch)
        if not ok:
            raise RuntimeError(err)

    def run(self):
        """Serve commands until the session is closed or has been idle too long. Returns why it stopped."""
        idle_since = time.monotonic()
        delay = POLL_MIN_SECONDS
        while True:
            reply = self.client.call("next_command", {})
            if not reply.get("accepted"):
                raise SessionRefused("Processa refused the poll")
            command = reply.get("command")
            if command is None:
                if time.monotonic() - idle_since > self.idle_timeout:
                    self.checkpoint()
                    return "idle"
                if self.unpushed and time.monotonic() - self.last_push > CHECKPOINT_SECONDS:
                    self.checkpoint()
                time.sleep(delay)
                delay = min(delay * 1.5, POLL_MAX_SECONDS)
                continue
            delay, idle_since = POLL_MIN_SECONDS, time.monotonic()
            status, result = self.execute(command)
            self.client.call("post_result", {"seq": command["seq"], "status": status, "result": result})
            if command["kind"] == "close":
                return "closed"

    def execute(self, command):
        """(status, result) for one command. An exception becomes an error result, so Processa always gets an answer."""
        timings = srv._Timings()
        try:
            handler = {"tool": self._tool, "run_tests": self._run_tests, "finalize": self._finalize, "close": self._close}[command["kind"]]
            status, result = handler(command, timings)
        except Exception as exc:  # noqa: BLE001 - reported to Processa as the command's result
            status, result = "error", {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}
        return status, {**result, "timings_ms": timings.report()}

    def _tool(self, command, timings):
        args = command["payload"].get("args") or {}
        with timings.span("tool"):
            result, changed = srv._apply_tool(self.app_dir, self.target_app, command["action"], args)
        if changed:
            with timings.span("git"):
                self._commit(changed, command["action"])
        return ("error" if result.get("error") else "ok"), result

    def _run_tests(self, command, timings):
        with timings.span("git"):
            self.checkpoint()
        if srv._needs_migrate(srv._changed_paths(self.target_app, self.git_branch)):
            with timings.span("migrate"):
                migrated, stdout, stderr = srv._migrate_site()
            if not migrated:
                return "ok", {"status": "failed", "error": stderr, "stdout_tail": stdout}
        with timings.span("tests"):
            passed, stdout, stderr, failing = srv._tests_for_tree(self.target_app, self.git_branch)
        return "ok", {
            "status": "tests_passed" if passed else "tests_failed",
            "stdout_tail": stdout, "stderr_tail": stderr, "failing_tests": failing,
            "commit": srv._head_commit(self.target_app),
        }

    def _finalize(self, command, timings):
        with timings.span("git"):
            self.checkpoint()
        payload = command["payload"]
        run_ctx = {
            "git_branch": self.git_branch, "work_item_description": payload.get("work_item_description", ""),
            "work_item_id": self.work_item, "github_token": self.github_token,
            "correlation_id": self.client.session_id, "agent_name": self.agent_name,
        }
        with timings.span("pr"):
            result = srv._tool_open_pull_request(self.target_app, payload.get("args") or {}, run_ctx)
        body = {"status": "tests_passed" if result.get("pr_url") else "tests_failed"}
        if result.get("pr_url"):
            body["pr_url"] = result["pr_url"]
        if result.get("error"):
            body["pr_error"] = result["error"]
        if result.get("failing_tests"):
            body["failing_tests"] = result["failing_tests"]
        return "ok", body

    def _close(self, command, timings):
        with timings.span("git"):
            self.checkpoint()
        return "ok", {}

    def _commit(self, changed, action):
        """Commit what a command changed on the local branch. It is pushed at the next checkpoint."""
        name, email = srv._git_identity_for(self.agent_name)
        for path in [changed] if isinstance(changed, str) else changed:
            srv._run(f"cd {shlex.quote(self.app_dir)} && git add -- {shlex.quote(path)}")
        committed = srv._run(
            f"cd {shlex.quote(self.app_dir)} && git -c user.email={shlex.quote(email)} -c user.name={shlex.quote(name)} "
            f"commit -q -m {shlex.quote(f'{action} via {self.agent_name}')}"
        )
        if committed.returncode == 0:
            self.unpushed = True

    def checkpoint(self):
        """Push the branch so a lost container can be resumed from it."""
        if not self.unpushed:
            return
        with srv._authed_remote(self.app_dir, self.github_token):
            pushed = srv._run(f"cd {shlex.quote(self.app_dir)} && git push origin {shlex.quote(self.head_branch)}")
        if pushed.returncode != 0:
            raise RuntimeError(f"git push of {self.head_branch} failed: {pushed.stderr[-300:].replace(self.github_token, '***') if self.github_token else pushed.stderr[-300:]}")
        self.unpushed = False
        self.last_push = time.monotonic()


def main():
    session_id, token, url = (os.environ.get(name, "").strip() for name in ("SESSION_ID", "SESSION_TOKEN", "PROCESSA_URL"))
    if not (session_id and token and url):
        print("[session_agent] SESSION_ID, SESSION_TOKEN and PROCESSA_URL are all required.")
        return 2
    check_processa_url(url)
    client = Processa(url, session_id)
    settings = client.call("register", {"token": token})
    if not settings.get("accepted"):
        print("[session_agent] Processa refused the registration.")
        return 1
    client.key = settings["session_key"]
    session = Session(client, settings)
    try:
        session.prepare()
        reason = session.run()
    except SessionRefused as exc:
        print(f"[session_agent] {exc}")
        return 1
    print(f"[session_agent] finished: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
