"""Local check of the session Job: the real session agent, as a separate process, against a local Processa.

Run it with the bench's own Python from the bench's sites folder, with bench start running:

    cd <bench>/sites && ../env/bin/python <agent-sandbox>/dev/local_session_check.py

Only the Cloud Run start is replaced: the session is created through Processa as usual, and the agent is launched here
with the session id and the one-time token. Everything else is real: HTTP to the local Processa, the command queue,
signed results, git against a throwaway bare repository, and the push at close. It creates one test session
(WI-E2E-1) and removes it, with its commands and the temporary repositories, when it finishes.

Environment: SANDBOX_CHECK_SITE (default onefm.localhost) and SANDBOX_CHECK_PROCESSA_URL (default http://<site>:8000).
"""
import json, os, shutil, subprocess, sys, tempfile, time
from unittest.mock import patch
import frappe
SITE = os.environ.get("SANDBOX_CHECK_SITE", "onefm.localhost")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
frappe.init(site=SITE, sites_path=os.getcwd()); frappe.connect()
from one_bpmn.one_bpmn.connectors import agent_sandbox_job as job, agent_sandbox_session as sessions

def git(cwd, *a): return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
root = os.path.realpath(tempfile.mkdtemp())
origin, seed, bench = (os.path.join(root, n) for n in ("origin.git", "seed", "bench"))
os.makedirs(seed); git(root, "init", "-q", "--bare", origin); git(seed, "init", "-q", "-b", "staging")
os.makedirs(os.path.join(seed, "one_bpmn")); open(os.path.join(seed, "one_bpmn/a.py"), "w").write("one two\n")
git(seed, "add", "-A"); git(seed, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "seed"); git(seed, "push", "-q", origin, "staging")
app = os.path.join(bench, "apps", "one_bpmn"); os.makedirs(os.path.dirname(app)); git(root, "clone", "-q", origin, app)
git(app, "config", "user.email", "a@b"); git(app, "config", "user.name", "t")

captured = {}
def fake_start(session_name, token):
    captured.update(session=session_name, token=token); return "local-execution"
frappe.flags.in_test = False
with patch.object(job, "start_execution", fake_start):
    session = sessions.ensure_session(work_item="WI-E2E-1", target_app="one_bpmn", git_branch="staging", agent_name="Dev Agent", caller_instance=None)
frappe.db.commit()
print("session", session.name, frappe.db.get_value(sessions.SESSION, session.name, "state"))

env = {**os.environ, "SESSION_ID": captured["session"], "SESSION_TOKEN": captured["token"], "PROCESSA_URL": os.environ.get("SANDBOX_CHECK_PROCESSA_URL", f"http://{SITE}:8000"), "BENCH_DIR": bench, "SANDBOX_ENV": "local"}
agent = subprocess.Popen([sys.executable, "session_agent.py"], cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
ok = True
def check(label, cond, extra=""):
    global ok; ok = ok and bool(cond); print(("PASS " if cond else "FAIL ") + label, extra)
try:
    cmd = sessions.enqueue(session, "tool", "read_file", {"args": {"path": "one_bpmn/a.py"}})
    answer = sessions.wait_for(cmd, timeout=90)
    check("first command is answered by the real agent over HTTP", answer and answer.get("content") == "one two\n", answer)
    check("session registered and active", frappe.db.get_value(sessions.SESSION, session.name, "state") in ("registered", "active"), frappe.db.get_value(sessions.SESSION, session.name, "state"))
    cmd = sessions.enqueue(session, "tool", "edit_file", {"args": {"path": "one_bpmn/a.py", "old_string": "two", "new_string": "2"}})
    answer = sessions.wait_for(cmd, timeout=60)
    check("an edit is applied", answer and answer.get("edited") is True, answer)
    check("the edit is committed locally and not yet on origin", "edit_file via Dev Agent" in git(app, "log", "-1", "--format=%s") and git(origin, "show", "WI-E2E-1:one_bpmn/a.py") == "one two")
    cmd = sessions.enqueue(session, "tool", "grep_files", {"args": {"pattern": "two", "literal": True}})
    answer = sessions.wait_for(cmd, timeout=60)
    check("a search runs on the checkout", answer and answer.get("matches") is not None, answer and answer.get("count"))
    seqs = frappe.get_all(sessions.COMMAND, filters={"session": session.name}, pluck="seq", order_by="seq asc")
    check("commands carry increasing sequence numbers", seqs == [1, 2, 3], seqs)
    sessions.close(frappe.get_doc(sessions.SESSION, session.name))
    code = agent.wait(timeout=60)
    check("close makes the agent push and exit cleanly", code == 0, code)
    check("the work reached origin at the checkpoint", git(origin, "show", "WI-E2E-1:one_bpmn/a.py") == "one 2")
    check("the session is closed", frappe.db.get_value(sessions.SESSION, session.name, "state") == "closed", frappe.db.get_value(sessions.SESSION, session.name, "state"))
finally:
    if agent.poll() is None: agent.kill()
    print("--- agent output ---"); print((agent.stdout.read() or "")[-800:])
    for name in frappe.get_all(sessions.COMMAND, filters={"session": session.name}, pluck="name"): frappe.delete_doc(sessions.COMMAND, name, force=1, ignore_permissions=True)
    frappe.delete_doc(sessions.SESSION, session.name, force=1, ignore_permissions=True); frappe.db.commit()
    shutil.rmtree(root, ignore_errors=True)
print("ALL PASS" if ok else "SOME FAILED")
