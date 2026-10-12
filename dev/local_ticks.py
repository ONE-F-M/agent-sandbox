"""Runs the jobs the scheduler would, for a local bench whose scheduler is off, every 20 seconds.

    cd <bench>/sites && ../env/bin/python <agent-sandbox>/dev/local_ticks.py [rounds]

It resumes agents waiting on a delegation, fires due timers, settles stuck sandbox runs, and starts, closes and settles
sandbox sessions (a queued session only starts when this runs).
"""
import os
import sys
import time

import frappe

frappe.init(site=os.environ.get("SANDBOX_CHECK_SITE", "onefm.localhost"), sites_path=os.getcwd())
frappe.connect()
frappe.set_user("Administrator")
from one_bpmn.one_bpmn.connectors.agent_sandbox_reaper import reap, reap_sessions
from one_bpmn.tasks import poll_a2a_tasks, process_timer_catch_events

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
for n in range(rounds):
	for job in (poll_a2a_tasks, process_timer_catch_events, reap, reap_sessions):
		job()
		frappe.db.commit()
	if n < rounds - 1:
		time.sleep(20)
print("ticks finished")
