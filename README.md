# agent-sandbox

A disposable Cloud Run sandbox for the "Dev Agent" feature in `one_bpmn`
(Processa). It **is** the coding agent: given a work order, a model, a live
API key, and a GitHub token — all supplied fresh in the request that
dispatches it — it checks out the target app's branch, runs its own bounded
read/write/test tool-calling loop against that model, and, on a passing
test run, opens the pull request itself, directly against GitHub. It carries
no system prompt, model choice, or credential of its own; everything arrives
per-dispatch from Processa's `dev_agent_sandbox_ops` connector.

This document is the full runbook: GitHub PAT → local bake → Cloud Run
deploy → IAM wiring → Processa Settings, in the order you actually run them.

Forked from `~/Desktop/onefm-ai-agent`'s infrastructure pattern (same
single-container bench-in-a-box mechanism, same build/deploy scripting
style) — but a completely separate project: separate service, separate
Artifact Registry repo, separate secrets, separate GCP service account.
Nothing here reads or writes anything belonging to `onefm-ai-agent`.

## How a dispatch actually works

1. Processa's `dispatch_to_sandbox` connector resolves `agent_config`
   (`system_prompt`, `model`, a live `api_key` — from the "Dev Agent" AI
   Agent Configuration and its linked AI Provider) and `github_token` (from
   Processa Settings), and POSTs all of it, plus `target_app` / `git_branch`
   / `work_item_description`, to `/run`.
2. `/run` validates the payload, returns `202` immediately, and does the
   actual work in a background thread:
   - `git fetch` + checkout the target branch, `bench migrate`.
   - Run the coding loop (`_run_coding_loop` in `dev_agent_server.py`): a
     bounded (30-turn) tool-calling loop against the given model, with four
     tools scoped to the checked-out working tree — `read_file`,
     `write_file`, `list_files`, `run_tests`. The model decides what to
     read, what to change, and when to test; nothing here plans on its
     behalf.
   - A final, mandatory `bench run-tests` pass — independent of whatever the
     loop did internally — is what actually decides pass/fail.
   - On a pass with real changes, `_open_pr` commits every changed file via
     GitHub's Contents API and opens a PR directly (see "Two GitHub tokens"
     below) — no push credentials or local git state needed for this part.
3. The sandbox POSTs an HMAC-signed callback back to Processa with the
   outcome (`tests_passed` / `tests_failed` / `tests_passed_no_changes`),
   the agent's own final report, and `pr_url` on success.

The sandbox never plans a change in advance and never sees the "shape" of a
work order beyond its own text — every decision (what to read, what to
write, when it's done) is the model's, made live, inside the loop.

## Two GitHub tokens — don't confuse them

- **`dev-agent-github-token`** (Secret Manager, wired at deploy time,
  injected as `GITHUB_TOKEN`) — used to clone the private `ONE-F-M` repos
  when the image is baked, and by the running container's own `git fetch`
  against those same repos on each dispatch. Needs **read-only** access.
- **`github_token`** (sent fresh in every dispatch payload, from `Processa
  Settings.github_token`) — used only by `_open_pr` to commit files and open
  a pull request via the GitHub REST API. Needs **read + write** (`repo`
  scope, or fine-grained Contents + Pull requests).

These are deliberately separate credentials with separate scopes — the
build/runtime clone identity should never be able to open a PR, and vice
versa.

## 0. Prerequisites

- `gcloud` CLI installed and authenticated:
  ```bash
  gcloud auth login
  ```
  ```bash
  gcloud config set project <PROJECT_ID>
  ```
  If `gcloud` isn't installed yet:
  ```bash
  brew install --cask google-cloud-sdk
  ```
- Docker installed and running locally (used for the bake — never for the actual deploy target, which is Cloud Run).
- A GCP project already hosting `onefm-agent`/`onefm-agent-v2` (or any project) — same project is fine, since every resource below uses names that never collide with those services.

## 1. Create a dedicated GitHub PAT (clone token)

Don't reuse `onefm-ai-agent`'s token or any personal PAT used elsewhere — a
compromised sandbox should only ever expose what it strictly needs.

1. GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**.
2. **Resource owner**: `ONE-F-M`.
3. **Repository access**: select only the private repos the 02a-02d clone stages
   actually clone: `one_fm`, `onefm_sso`, `one_fm_password_management` (repo name
   `password_management`), `one_bpmn`, `onefm_mcp`, `frappe_agile`, `one_lms`,
   `mobile_app_ionic`.
4. **Permissions → Repository permissions → Contents: Read-only.** Nothing else. This
   token only ever clones/fetches — it is never the one used to open a PR (see
   "Two GitHub tokens" above; that's a separate credential, configured in
   Processa Settings, not here).
5. Copy the token now — GitHub won't show it again. Don't paste it into any chat or commit it anywhere.

## 2. Local `.env` for the bake

```bash
cd ~/Desktop/agent-sandbox && cp .env.example .env
```
Open `.env` and set `GITHUB_TOKEN=` to the PAT from step 1. This copy is only
ever used locally to clone the private repos during the bake — it's separate
from the copy that ends up in Secret Manager in step 3. **Never commit this
file** — it's already in `.gitignore`.

## 3. Create the two Secret Manager secrets

```bash
gcloud secrets create dev-agent-github-token --data-file=- --project=<PROJECT_ID>
```
Paste the same PAT, then **Ctrl+D** (not Enter — this ends input without a trailing newline).

```bash
openssl rand -hex 32 | gcloud secrets create dev-agent-callback-secret --data-file=- --project=<PROJECT_ID>
```

If either command says the secret already exists (e.g. from a prior attempt), use `versions add` instead of `create` — you can't `create` a secret name twice, but you can always add a new version to update its value:
```bash
gcloud secrets versions add dev-agent-github-token --data-file=- --project=<PROJECT_ID>
```

**Note on `openssl rand -hex 32`:** it prints a trailing newline, which gets
stored as part of the secret's bytes if you pipe it straight in. Both sides
of the HMAC check (`dev_agent_server.py` and `dev_agent_callback.py` in
Processa) `.strip()` defensively, so this is no longer a live footgun — but
if you ever need to update the value from a shell one-liner, strip it
yourself to be safe: `printf '%s' "$SECRET" | gcloud secrets versions add ...`.

## 4. Grant the Cloud Run runtime identity access to both secrets

```bash
gcloud projects describe <PROJECT_ID> --format='value(projectNumber)'
```

```bash
gcloud secrets add-iam-policy-binding dev-agent-github-token --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=<PROJECT_ID>
```
```bash
gcloud secrets add-iam-policy-binding dev-agent-callback-secret --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=<PROJECT_ID>
```

## 5. Bake the image

```bash
cd ~/Desktop/agent-sandbox && ENV=production ./bake_image.sh
```

Expect 40–75 minutes on a clean run (Docker layer caching makes every rebake
after the first noticeably faster, as long as only late layers like
`dev_agent_server.py`/`entrypoint.sh` changed) — this clones and pins all 18
apps, installs dependencies, and boots a throwaway site to prove the whole
thing works before committing the image. The build is split into six
separate Docker layers (`01_init_bench.sh` → `02a_clone_public_apps.sh` →
`02b_clone_onefm_apps_1.sh` → `02c_clone_onefm_apps_2.sh` →
`02d_clone_lms_and_mobile.sh` → `03_install_requirements.sh`) — the four
clone stages specifically so a dropped connection while pushing the built
image only costs re-uploading whichever one stage's layer failed, not a
single ~6.4GB all-apps layer starting over from zero.

Use `ENV=beta` instead everywhere in this doc for a beta deploy — see
"Beta vs. production" below.

Once it succeeds, it prints:
```
Golden Image created: dev-agent-sandbox:baked-production
Deploy with: python3 deploy.py --env production --use-baked
```

## 6. Deploy to Cloud Run

```bash
cd ~/Desktop/agent-sandbox && python3 deploy.py --env production --project <PROJECT_ID> --use-baked
```

This pushes the baked image to Artifact Registry and runs `gcloud run deploy
dev-agent-sandbox` — the single command that both creates the service (first
run) and updates it (every run after). No prior "create the service" step in
the Console is needed; this command is what brings it into existence. It's
deployed **without** `--allow-unauthenticated`, so Cloud Run's own IAM layer
gates every call — nothing reaches `/run` without a valid identity token.

It also deploys with `--min-instances=1` — one instance is kept warm at all
times (a small standing cost), rather than scaling to zero. This is required,
not optional: `/run` returns `202` immediately and keeps doing the actual
work (checkout/migrate/coding-loop/run-tests, often 10+ minutes) in a
background thread Cloud Run can't see. With `min-instances=0` the autoscaler
reclaims the "idle" instance mid-job — this is exactly what killed an early
real run (dispatched, then the instance was torn down ~15 minutes later,
every subsequent `/status` check 404'ing against a fresh cold instance with
no memory of the job). See the comment above `CLOUD_RUN_MIN_INST` in
`deploy.py` for the full trace. If you ever redeploy an older revision or
hand-edit the Cloud Run service outside this script, make sure
min-instances stays at 1.

Note the printed **Service URL** — you'll need it in step 10.

## 7. Create the service account the connector authenticates as

This is a separate, non-human identity from your own GCP login and from the
service's own runtime identity — code running unattended (Processa's
background job) needs something to authenticate *as*.

```bash
gcloud iam service-accounts create dev-agent-caller --project=<PROJECT_ID> --display-name="Dev Agent connector caller"
```

## 8. Grant it permission to invoke the sandbox

```bash
gcloud run services add-iam-policy-binding dev-agent-sandbox --region=us-central1 --project=<PROJECT_ID> --member="serviceAccount:dev-agent-caller@<PROJECT_ID>.iam.gserviceaccount.com" --role="roles/run.invoker"
```

(Optional, for your own manual testing via the Console's TESTING tab or `curl`:)
```bash
gcloud run services add-iam-policy-binding dev-agent-sandbox --region=us-central1 --project=<PROJECT_ID> --member="user:<your-gcp-email>" --role="roles/run.invoker"
```

## 9. Download the service account's key

A real credential — never commit it, and keep it outside any git-tracked folder.

```bash
gcloud iam service-accounts keys create ~/dev-agent-caller-key.json --iam-account=dev-agent-caller@<PROJECT_ID>.iam.gserviceaccount.com
```

## 10. Point the bench at the key and the sandbox

```bash
bench --site <your-site> set-config dev_agent_gcp_service_account_key_path ~/dev-agent-caller-key.json
```

Then, in the desk UI, open **Processa Settings**:

- **Dev Agent Sandbox → Sandbox URL**: the Service URL printed in step 6 (e.g. `https://dev-agent-sandbox-<hash>.<region>.run.app`)
- **Dev Agent Sandbox → Callback Secret**: run this and paste the output:
  ```bash
  gcloud secrets versions access latest --secret=dev-agent-callback-secret --project=<PROJECT_ID>
  ```
  Must be the *exact* value from Secret Manager — the sandbox signs callbacks with this same secret, and a mismatch means every callback gets silently rejected.
- **GitHub Integration → GitHub Access Token**: a *separate* PAT with `repo`
  (read + write) scope — this is the one `_open_pr` uses to actually commit
  files and open the PR. Sent fresh with every dispatch; the sandbox never
  stores it. This field is hidden by default behind the **Connect to
  Production** checkbox on Processa Settings — check that first if you don't
  see the GitHub Integration section (note: that checkbox also gates whether
  process-map changes require an Active Production Process Implementation —
  know what else you're turning on before you flip it).
- The "Dev Agent" **AI Agent Configuration**'s `ai_model` field decides which
  model the coding loop runs — the credential for it is resolved
  automatically from that model's linked **AI Provider**, the same
  credential store every other in-Processa agent already uses. Nothing
  sandbox-specific to configure here beyond picking the right model.

## 11. Verify

Console → **Cloud Run → dev-agent-sandbox → TESTING** → send `GET /health` →
expect `{"status": "ok"}`.

## 12. (Local testing only) Expose Processa for the callback

The sandbox's callback needs an `https://` URL reachable from Google's
network — not `localhost`. For local dev testing:

```bash
ngrok http <webserver_port>
```
(check `common_site_config.json`'s `webserver_port` — do not assume 8000/80 without checking)

```bash
bench --site <your-site> set-maintenance-mode off
```
```bash
bench --site <your-site> set-config host_name https://<your-ngrok-subdomain>.ngrok-free.dev
```

`host_name` is what `frappe.utils.get_url()` resolves against outside of a
live web request (i.e. from a background job) — without it, the callback URL
the connector builds won't point anywhere reachable. Note ngrok's free tier
issues a new random subdomain every time it restarts — update `host_name`
again whenever that happens, or the callback will silently fail to reach you.

## Beta vs. production

Every step above has a beta equivalent — swap `production` for `beta` and the
`-production` names for `-beta` ones (`dev-agent-github-token-beta`,
`dev-agent-callback-secret-beta`, `dev-agent-sandbox-beta`,
`dev-agent-sandbox:baked-beta`). They're fully independent: separate image
tag, separate secrets, separate service — a beta deploy can never overwrite
production or vice versa. `deploy.py` and `bake_image.sh` both default to
`beta` specifically as a guard against an accidental production push, so
production always has to be requested explicitly with `--env production` /
`ENV=production`.

## Rebaking after a code change

`entrypoint.sh`, `dev_agent_server.py`, and the Dockerfile are all baked into
the image at build time — a change to any of them needs a fresh
`./bake_image.sh` + `deploy.py --use-baked` cycle before it takes effect.
Nothing about the running container watches these files for changes.

## Known gaps

- **Tests need `allow_tests` on the sandboxed site.** Without it, `bench
  run-tests` prints "Testing is disabled for the site!" and exits `0`
  without running anything — every dispatch would silently report
  `tests_passed` regardless of what changed. This is set during site
  creation in `entrypoint.sh`'s `create_and_install_site()` — confirm it's
  still there if you ever touch that function.
- **`erpnext`'s global `before_tests` hook** creates a default Company (and,
  via ERPNext core, default Warehouses) the first time any test suite runs
  on a fresh site — for *any* target app, not just the one being tested.
  If any installed app's own Warehouse customization depends on a field
  that isn't packaged as a fixture, this crashes before a single real test
  runs. Confirmed live via a missing `one_fm` custom field; fix belongs in
  that app's own repo (export the field as a fixture), not here.
- **No coverage/lint step.** The coding loop's only signal is
  pass/fail from the target app's existing test suite — it doesn't run a
  linter or report coverage.
- **Retry-on-failure is available but unproven end-to-end.** The coding
  loop's tools let the model call `run_tests`, see a failure, revise a
  file, and retry — all within one dispatch — but every real failure seen
  in testing so far has been a pre-existing, unrelated environment issue
  the model correctly declined to "fix." The fail → revise → pass cycle is
  architecturally sound but hasn't been observed completing for real yet.
