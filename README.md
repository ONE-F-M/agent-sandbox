# dev-agent-sandbox

A disposable, config-free Cloud Run sandbox for the "AI Dev Agent" feature in
`one_bpmn`. It carries no agent identity of its own — every piece of behavior
(system prompt, tools, skills) is resolved by Processa's `dev_agent_sandbox_ops`
connector at dispatch time and handed over as a request payload. This
document is the full runbook: GitHub PAT → local bake → Cloud Run deploy →
IAM wiring → Processa Settings, in the order you actually run them.

Forked from `~/Desktop/onefm-ai-agent`'s infrastructure pattern (same
single-container bench-in-a-box mechanism, same build/deploy scripting
style) — but a completely separate project: separate service, separate
Artifact Registry repo, separate secrets, separate GCP service account.
Nothing here reads or writes anything belonging to `onefm-ai-agent`.

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

## 1. Create a dedicated GitHub PAT

Don't reuse `onefm-ai-agent`'s token or any personal PAT used elsewhere — a
compromised sandbox should only ever expose what it strictly needs.

1. GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**.
2. **Resource owner**: `ONE-F-M`.
3. **Repository access**: select only the repos `02_clone_apps.sh` actually clones:
   `one_fm`, `onefm_sso`, `one_fm_password_management` (repo name `password_management`),
   `one_bpmn`, `onefm_mcp`, `frappe_agile`.
4. **Permissions → Repository permissions → Contents: Read-only.** Nothing else. The
   sandbox only clones; it never pushes (PRs are opened by Processa's
   `dev_agent_callback.py` via `open_customization_pr()`, using a *different*,
   already-existing GitHub token from `Processa Settings.github_token` — not
   this one).
5. Copy the token now — GitHub won't show it again. Don't paste it into any chat or commit it anywhere.

## 2. Local `.env` for the bake

```bash
cd ~/Desktop/dev-agent-sandbox && cp .env.example .env
```
Open `.env` and set `GITHUB_TOKEN=` to the PAT from step 1. This copy is only
ever used locally to clone the private repos during the bake — it's separate
from the copy that ends up in Secret Manager in step 3.

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
cd ~/Desktop/dev-agent-sandbox && ENV=production ./bake_image.sh
```

Expect ~40 minutes on a clean run — this clones and pins all 15 apps, installs
dependencies, and boots a throwaway site to prove the whole thing works before
committing the image. The build is split into three separate Docker layers
(`01_init_bench.sh` → `02_clone_apps.sh` → `03_install_requirements.sh`)
specifically so a failure late in the process doesn't force re-cloning
everything on retry — only the layer that actually changed re-runs.

Use `ENV=beta` instead everywhere in this doc for a beta deploy — see
"Beta vs. production" below.

Once it succeeds, it prints:
```
Golden Image created: dev-agent-sandbox:baked-production
Deploy with: python3 deploy.py --env production --use-baked
```

## 6. Deploy to Cloud Run

```bash
cd ~/Desktop/dev-agent-sandbox && python3 deploy.py --env production --project <PROJECT_ID> --use-baked
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
work (checkout/migrate/run-tests, often 10+ minutes) in a background thread
Cloud Run can't see. With `min-instances=0` the autoscaler reclaims the
"idle" instance mid-job — this is exactly what killed run `DAS-91629` on
2026-08-25 (dispatched 15:24:35Z, instance torn down 15:39:42Z, every later
`/status` check 404'd against a fresh cold instance with no memory of the
job). See the comment above `CLOUD_RUN_MIN_INST` in `deploy.py` for the full
trace. If you ever redeploy an older revision or hand-edit the Cloud Run
service outside this script, make sure min-instances stays at 1.

Note the printed **Service URL** — you'll need it in step 9.

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

Then, in the desk UI, open **Processa Settings** → **Dev Agent Sandbox** section:

- **Sandbox URL**: the Service URL printed in step 6 (e.g. `https://dev-agent-sandbox-<hash>.<region>.run.app`)
- **Callback Secret**: run this and paste the output:
  ```bash
  gcloud secrets versions access latest --secret=dev-agent-callback-secret --project=<PROJECT_ID>
  ```
  Must be the *exact* value from Secret Manager — the sandbox signs callbacks with this same secret, and a mismatch means every callback gets silently rejected.

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
the connector builds won't point anywhere reachable.

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

## What this doesn't do yet

- **The coding-agent loop itself** — `dev_agent_server.py`'s `run_job()` still
  runs the target app's *existing* test suite unmodified; the actual
  code-writing logic is stubbed.
- **The "Dev Agent" BPMN diagram** — has to be authored by hand in `/spiff`;
  it's not something a patch can seed (same convention the Connector Agent's
  own map follows).
- **Reviewer feedback loop** — a PR comment currently reaches nobody. See
  "Phase 3" in the project plan for the sketched design (a GitHub webhook
  re-dispatching to the same branch, not a new PR).
- **`min-instances=1` is a cost/simplicity trade-off, not the only fix** for
  the background-thread-survives-the-response problem above. The more robust
  alternative — moving `dispatch()`'s outbound call off the live web request
  via `frappe.enqueue` on the `bpmn_ai_agent` queue, and making `/run` block
  until the job finishes instead of firing an async callback — would remove
  the standing cost and the callback/HMAC-signing mechanism entirely, but is
  a materially bigger change to already-tested code. Worth revisiting if the
  standing cost becomes a concern.
