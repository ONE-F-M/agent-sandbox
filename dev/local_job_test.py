"""Switches the local Processa to run an agent's sandbox work in Cloud Run Job sessions, or back, and shows the state.

Run it with the bench's own Python from the bench's sites folder:

    cd <bench>/sites && ../env/bin/python <agent-sandbox>/dev/local_job_test.py status
    cd <bench>/sites && ../env/bin/python <agent-sandbox>/dev/local_job_test.py on [--settings-only] [--agent "Dev Agent"] [--max-parallel 3]
    cd <bench>/sites && ../env/bin/python <agent-sandbox>/dev/local_job_test.py off

`on` fills the Sandbox Job fields in Processa Settings (project, region, job name, the Processa URL the container
calls, and the parallel cap) and ticks Sandbox Use Job on the one agent. `off` unticks it on every agent.
Nothing falls back: with the box ticked and no deployed Job, that agent's sandbox calls fail until you run `off`.
"""
import argparse
import os

import frappe

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=("status", "on", "off"))
parser.add_argument("--agent", default="Dev Agent")
parser.add_argument("--max-parallel", type=int, default=3)
parser.add_argument("--project", default="ai-agents-10")
parser.add_argument("--region", default="us-central1")
parser.add_argument("--job", default="dev-agent-sandbox-job")
parser.add_argument("--processa-url", default="")
parser.add_argument("--settings-only", action="store_true", help="fill the settings but do not tick any agent")
args = parser.parse_args()

frappe.init(site=os.environ.get("SANDBOX_CHECK_SITE", "onefm.localhost"), sites_path=os.getcwd())
frappe.connect()
SETTINGS = "Processa Settings"
FIELDS = ("sandbox_job_project", "sandbox_job_region", "sandbox_job_name", "sandbox_job_processa_url", "sandbox_job_max_parallel")


def ticked():
	return frappe.get_all("AI Agent Configuration", filters={"sandbox_use_job": 1}, pluck="name")


if args.action == "on":
	url = args.processa_url or frappe.conf.get("host_name") or ""
	if not url.startswith("https://"):
		raise SystemExit(f"The Processa URL must be https (got {url!r}). Set host_name to your ngrok address or pass --processa-url.")
	frappe.db.set_single_value(
		SETTINGS,
		{
			"sandbox_job_project": args.project, "sandbox_job_region": args.region, "sandbox_job_name": args.job,
			"sandbox_job_processa_url": url, "sandbox_job_max_parallel": args.max_parallel,
		},
	)
	if not args.settings_only:
		frappe.db.set_value("AI Agent Configuration", args.agent, "sandbox_use_job", 1, update_modified=False)
elif args.action == "off":
	for name in ticked():
		frappe.db.set_value("AI Agent Configuration", name, "sandbox_use_job", 0, update_modified=False)
frappe.db.commit()
frappe.clear_cache()

print("Processa Settings:", {field: frappe.db.get_single_value(SETTINGS, field) for field in FIELDS})
print("Sandbox Use Job ticked on:", ticked() or "no agent (every call is its own request to the shared service)")
live = frappe.db.count("Agent Sandbox Session", {"state": ["in", ["starting", "registered", "active", "closing"]]})
print("Live sessions:", live, "| queued:", frappe.db.count("Agent Sandbox Session", {"state": "queued"}))
