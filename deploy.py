"""
Dev-Agent Sandbox — Cloud Run Deployment Script
================================================
Forked from onefm-ai-agent's deploy_native.py — same build/push/deploy
scripting style and the same proven `--concurrency=1` isolation pattern, but
deploying a *separate* service with no agent logic baked in: this sandbox
receives its entire agent configuration (system prompt, tools, skills) per
request from Processa. See dev_agent_server.py.

Mirrors onefm-ai-agent's beta/production split (its `onefm-agent` /
`onefm-agent-v2` services): --env selects everything that must never collide
between the two — service name, pushed image tag, and secret names — so a
beta deploy can never overwrite production's image or read/write its
secrets. This is deliberately keyed off one flag rather than three separate
ones you could set inconsistently.

This script only builds/pushes/deploys — it does not read or share any
secrets, image, or code from onefm-ai-agent.

Stages:
  1. Build the sandbox image (Dockerfile.frappe_runtime — bench + all 15 apps
     + the generic driver, single stage; no separate overlay build needed
     since there is no agent-specific content to layer on top).
  2. Ensure the Artifact Registry repository exists.
  3. Tag and push the image to Artifact Registry.
  4. Deploy to Google Cloud Run via `gcloud run deploy`.

Requirements:
  - Docker installed and running locally.
  - `gcloud` CLI installed and authenticated (`gcloud auth login`).
  - Docker configured for the registry (`gcloud auth configure-docker <LOCATION>-docker.pkg.dev`).
  - Secrets already created in GCP Secret Manager (see _ENVIRONMENTS below)
    — this script does not create them.
"""

import os
import subprocess
import argparse

REPOSITORY = "dev-agent-sandbox-repository"
IMAGE_TAG = "dev-agent-sandbox"

# Everything that must stay distinct between environments lives here, keyed
# together, so choosing --env is the only decision — there's no way to end
# up with production's service name paired with beta's secrets by accident.
_ENVIRONMENTS = {
    "production": {
        "service": "dev-agent-sandbox",
        "secrets": {
            "GITHUB_TOKEN": "dev-agent-github-token",
            "CALLBACK_HMAC_SECRET": "dev-agent-callback-secret",
        },
    },
    "beta": {
        "service": "dev-agent-sandbox-beta",
        "secrets": {
            "GITHUB_TOKEN": "dev-agent-github-token-beta",
            "CALLBACK_HMAC_SECRET": "dev-agent-callback-secret-beta",
        },
    },
}

# Cloud Run tuning — same shape as onefm-ai-agent's proven config; adjust to
# your GCP quota. --concurrency=1 is the isolation boundary: one work item's
# sandbox per container instance, never shared.
CLOUD_RUN_MEMORY = "8Gi"
CLOUD_RUN_CPU = "4"
CLOUD_RUN_PORT = "8080"
CLOUD_RUN_MAX_INST = "3"
# Must stay >= 1. dev_agent_server.py's /run handler returns 202 immediately
# and keeps running the actual job (checkout/migrate/run-tests, often 10+
# minutes) in a detached background thread — Cloud Run has no visibility into
# that thread, only into the HTTP request/response cycle, which completes in
# milliseconds. With min-instances=0 the autoscaler sees the instance go idle
# right after the 202 and reclaims it mid-job: confirmed against real Cloud
# Run logs for run DAS-91629 (dispatched 2026-08-25T15:24:35Z, instance
# session closed 2026-08-25T15:39:42Z — ~15 minutes in, well before a
# migrate+run-tests pass on a large app like one_bpmn could finish), after
# which every later /status check 404'd against a fresh cold instance that
# had never heard of the correlation_id. min-instances=1 keeps an instance
# permanently warm so the background thread is never killed out from under
# itself. dispatch_connector runs inline in the caller's live web request
# (see dev_agent_sandbox_ops.py's docstring) with only a 30s HTTP timeout, so
# making /run itself block until the job finishes is not an option here —
# this is the correct fix at this layer, not a workaround.
CLOUD_RUN_MIN_INST = "1"
CLOUD_RUN_TIMEOUT = "3600"


def run(command: str, description: str) -> None:
    print(f"\n{'=' * 60}\n  {description}\n{'=' * 60}")
    result = subprocess.run(command, shell=True)
    if result.returncode != 0:
        print(f"\n[ERROR] Step failed: {description}")
        exit(result.returncode)


def _get_gcloud_project() -> str:
    try:
        result = subprocess.run(
            "gcloud config get-value project",
            shell=True, capture_output=True, text=True, timeout=10,
        )
        project = result.stdout.strip()
        if project and result.returncode == 0:
            return project
    except Exception:
        pass
    return ""


def deploy(
    env: str,
    project_id: str,
    location: str = "us-central1",
    use_baked: bool = False,
    callback_host: str = "",
) -> None:
    if env not in _ENVIRONMENTS:
        print(f"[ERROR] --env must be one of {list(_ENVIRONMENTS)}, got '{env}'.")
        exit(1)
    if not project_id:
        print("[ERROR] --project is required (or set a default via `gcloud config set project <ID>`).")
        exit(1)

    service_name = _ENVIRONMENTS[env]["service"]
    secret_names = _ENVIRONMENTS[env]["secrets"]

    registry = f"{location}-docker.pkg.dev"
    # The docker tag is the env-specific service name, not a shared `:latest`
    # — beta and production pushes can never overwrite each other's image.
    remote_image = f"{registry}/{project_id}/{REPOSITORY}/{IMAGE_TAG}:{service_name}"
    baked_tag = f"{IMAGE_TAG}:baked-{env}"

    if use_baked:
        print(f"\n[INFO] Using BAKED image '{baked_tag}' — skipping build (run ./bake_image.sh ENV={env} first if stale).")
        check_baked = subprocess.run(f"docker image inspect {baked_tag} >/dev/null 2>&1 || docker image inspect docker.io/library/{baked_tag} >/dev/null 2>&1", shell=True)
        if check_baked.returncode != 0:
            print(f"[ERROR] Baked image '{baked_tag}' not found. Run: ENV={env} ./bake_image.sh")
            exit(1)
        source_image = baked_tag
    else:
        os.environ["DOCKER_BUILDKIT"] = "1"
        run(
            f"docker build --platform linux/amd64 "
            f"--secret id=github_token,env=GITHUB_TOKEN "
            f"-f Dockerfile.frappe_runtime "
            f"-t {IMAGE_TAG}:{env} .",
            f"STAGE 1 / 4 — Building sandbox image for '{env}' (bench + 15 apps + generic driver)",
        )
        source_image = f"{IMAGE_TAG}:{env}"

    print(f"\n{'=' * 60}\n  STAGE 2 / 4 — Ensuring Artifact Registry Repository\n{'=' * 60}")
    result = subprocess.run(
        f"gcloud artifacts repositories create {REPOSITORY} "
        f"--repository-format=docker --location={location} --project={project_id}",
        shell=True,
    )
    if result.returncode != 0:
        print("[INFO] Repository already exists — continuing.")

    run(f"gcloud auth configure-docker {registry} --quiet", "Authenticating Docker with GCP")
    run(f"docker tag {source_image} {remote_image}", "Tagging image")
    run(f"docker push {remote_image}", f"STAGE 3 / 4 — Pushing '{env}' image to Artifact Registry")

    # All configuration from GCP Secret Manager, injected at runtime.
    # Prerequisites (run once per environment, not by this script):
    #   gcloud secrets create <secret-name> --data-file=- --project=<PROJECT_ID>
    #   Grant roles/secretmanager.secretAccessor to the Cloud Run service account.
    secrets = ",".join(f"{env_key}={secret_name}:latest" for env_key, secret_name in secret_names.items())
    env_var_list = [
        "PYTHONUNBUFFERED=1",
        f"SKIP_SETUP={'1' if use_baked else '0'}",
        f"SANDBOX_ENV={env}",
    ]
    if callback_host:
        # Pins this environment's sandbox to only ever call back to one host
        # — a beta sandbox can't be pointed at production's Processa (or
        # anywhere else) by a malformed/malicious /run payload. Optional
        # because you may not have this bench's public hostname yet.
        env_var_list.append(f"ALLOWED_CALLBACK_HOST={callback_host}")
    else:
        print(f"[WARN] --callback-host not set for '{env}' — the sandbox will accept a callback_url to any https host.")
    env_vars = ",".join(env_var_list)

    run(
        f"gcloud run deploy {service_name} "
        f"--image={remote_image} "
        f"--region={location} "
        f"--platform=managed "
        f"--port={CLOUD_RUN_PORT} "
        f"--memory={CLOUD_RUN_MEMORY} "
        f"--cpu={CLOUD_RUN_CPU} "
        f"--max-instances={CLOUD_RUN_MAX_INST} "
        f"--min-instances={CLOUD_RUN_MIN_INST} "
        f"--timeout={CLOUD_RUN_TIMEOUT} "
        f"--concurrency=1 "
        f"--no-cpu-throttling "
        f'--set-env-vars="{env_vars}" '
        f'--set-secrets="{secrets}" '
        f"--project={project_id} "
        f"--quiet",
        f"STAGE 4 / 4 — Deploying '{env}' to Google Cloud Run",
    )

    print("\n" + "=" * 60)
    print(f"  DEPLOYMENT COMPLETE — {env}")
    print("=" * 60)
    print(f"\n  Service : {service_name}")
    print(f"  Region  : {location}")
    print(f"  Image   : {remote_image}")
    print(f"  Secrets : {', '.join(secret_names.values())}")
    print(f"  Callback signing: {'on' if 'CALLBACK_HMAC_SECRET' in secret_names else 'MISSING — fix _ENVIRONMENTS'}")
    print(f"  Callback host pin: {callback_host or '(none — accepts any https callback_url)'}")
    print(f"\n  NOTE: this deploy does not pass --allow-unauthenticated, so Cloud Run's")
    print(f"  own IAM layer gates who can call POST /run — grant roles/run.invoker on")
    print(f"  {service_name} to whichever identity Processa's dispatcher authenticates as.")
    print(f"\n  Get this environment's URL (store it as Processa's")
    print(f"  dev_agent_sandbox_url for the {env} bench, not shared with the other env):")
    print(f"  gcloud run services describe {service_name} --region={location} --project={project_id} --format='value(status.url)'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy the dev-agent sandbox to Cloud Run")
    parser.add_argument("--env", choices=list(_ENVIRONMENTS), default="beta", help="Which environment to deploy — defaults to beta, never production, to avoid an accidental prod push")
    parser.add_argument("--project", default=_get_gcloud_project())
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--use-baked", action="store_true", help="Deploy the pre-baked golden image for this env (see bake_image.sh) instead of building fresh")
    parser.add_argument("--callback-host", default="", help="Hostname of this environment's Processa bench — pins ALLOWED_CALLBACK_HOST so the sandbox can't be pointed elsewhere")
    args = parser.parse_args()
    deploy(
        env=args.env,
        project_id=args.project,
        location=args.region,
        use_baked=args.use_baked,
        callback_host=args.callback_host,
    )
