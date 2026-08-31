#!/bin/bash
# bake_image.sh
# ==============================================================================
# Forked from onefm-ai-agent's bake_image.sh — same "Golden Image" mechanism,
# minus the ChromaDB/RAG indexing stages (this sandbox has no knowledge base
# of its own; it's config-free — everything comes from Processa per request).
#
# Without this, entrypoint.sh redoes the full site-creation + 15-app-install +
# migrate sequence on EVERY container start, because each Cloud Run instance
# is a fresh, disposable filesystem — there's nothing to persist between
# triggers unless it's baked into the image itself.
#
# 1. Runs the sandbox container locally with full setup (SKIP_SETUP=0).
# 2. Waits for MariaDB, the 'sandbox' site, and dev_agent_server.py to be up.
# 3. Commits the container state (site + DB data included) to:
#    dev-agent-sandbox:baked-<ENV>
#
# ENV selects which environment this bake is for — beta or production —
# matching deploy.py's --env. Defaults to beta so a bare `./bake_image.sh`
# can never accidentally produce something deploy.py would push as
# production. Run `ENV=production ./bake_image.sh` explicitly when that's
# actually intended.
# ==============================================================================

set -e

ENV="${ENV:-beta}"
if [ "$ENV" != "beta" ] && [ "$ENV" != "production" ]; then
    echo "[ERROR] ENV must be 'beta' or 'production', got '$ENV'."
    exit 1
fi

if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo "Error: .env file not found. Please create one from .env.example."
    exit 1
fi

IMAGE_NAME="dev-agent-sandbox"
BAKED_IMAGE_NAME="dev-agent-sandbox:baked-${ENV}"
CONTAINER_NAME="dev-agent-sandbox-bake-temp-${ENV}"

echo "--- [BAKE] Starting Golden Image creation for '${ENV}' ---"

# ---------------------------------------------------------------------------
# Fast path: CODE_ONLY=1 — update dev_agent_server.py / entrypoint.sh in an
# existing baked image without re-running MariaDB setup, site creation, or
# bench migrate. Use when ONLY the driver code changed, not the app list.
# ---------------------------------------------------------------------------
if [ "${CODE_ONLY:-0}" = "1" ]; then
    echo "--- [BAKE] CODE_ONLY=1 — fast code-only re-bake ---"

    if ! docker image inspect $BAKED_IMAGE_NAME > /dev/null 2>&1; then
        echo "[ERROR] No existing baked image '$BAKED_IMAGE_NAME' found."
        echo "        Run a full bake first: ENV=${ENV} ./bake_image.sh"
        exit 1
    fi

    docker rm -f $CONTAINER_NAME > /dev/null 2>&1 || true
    docker create --name $CONTAINER_NAME $BAKED_IMAGE_NAME

    echo "  Copying dev_agent_server.py ..."
    docker cp dev_agent_server.py $CONTAINER_NAME:/home/frappe/dev_agent_server.py
    echo "  Copying entrypoint.sh ..."
    docker cp entrypoint.sh $CONTAINER_NAME:/entrypoint.sh

    docker start $CONTAINER_NAME
    sleep 2
    docker exec $CONTAINER_NAME bash -c "
        chown frappe:frappe /home/frappe/dev_agent_server.py
        sed -i 's/\r\$//' /entrypoint.sh
        chmod +x /entrypoint.sh
    "
    docker stop $CONTAINER_NAME

    echo "  Committing updated image..."
    docker commit --change 'ENTRYPOINT ["/entrypoint.sh"]' $CONTAINER_NAME $BAKED_IMAGE_NAME
    docker rm $CONTAINER_NAME

    echo "--- [BAKE] Code-only re-bake complete: $BAKED_IMAGE_NAME ---"
    echo "Deploy with: python3 deploy.py --env ${ENV} --use-baked"
    exit 0
fi

# ── Full Bake Flow ────────────────────────────────────────────────────────────
BAKE_START=$SECONDS
BUILD_TAG="${IMAGE_NAME}:${ENV}"

if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "Stage 1 — Docker build: skipped (SKIP_BUILD=1)"
else
    T=$SECONDS
    DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -t $BUILD_TAG \
        --secret id=github_token,env=GITHUB_TOKEN \
        -f Dockerfile.frappe_runtime .
    echo "Stage 1 — Image build: $((SECONDS - T))s"
fi

docker rm -f $CONTAINER_NAME > /dev/null 2>&1 || true

echo "Launching container from $BUILD_TAG (SKIP_SETUP=0)..."
T=$SECONDS
docker run -d --name $CONTAINER_NAME \
    --platform linux/amd64 \
    -e SKIP_SETUP=0 \
    -e GITHUB_TOKEN=$GITHUB_TOKEN \
    -p 8080:8080 \
    $BUILD_TAG
echo "Stage 2 — Container launched: $((SECONDS - T))s"

# Poll until: (1) site_config.json exists, (2) the driver's health endpoint
# is up. This is the one-time expensive setup we're baking so future runs
# skip it entirely.
MAX_RETRIES=120
COUNT=0
T=$SECONDS
echo "Waiting for site 'sandbox' and dev_agent_server.py to be ready..."
until docker exec $CONTAINER_NAME bash -c "[[ -f /home/frappe/frappe-bench/sites/sandbox/site_config.json ]] && curl -s -f http://localhost:8080/health > /dev/null 2>&1"; do
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo ""
        echo "Error: Initialization timed out after 30 minutes."
        docker logs $CONTAINER_NAME
        docker stop $CONTAINER_NAME
        exit 1
    fi
    printf "."
    sleep 15
    COUNT=$((COUNT + 1))
done
echo ""
echo "Stage 3 — Site initialization: $((SECONDS - T))s"

T=$SECONDS
docker stop $CONTAINER_NAME
echo "Stage 4a — Container stop: $((SECONDS - T))s"

T=$SECONDS
docker commit $CONTAINER_NAME $BAKED_IMAGE_NAME
echo "Stage 4b — Docker commit: $((SECONDS - T))s"

docker rm $CONTAINER_NAME

echo "════════════════════════════════════════════════════════════"
echo "Golden Image created: $BAKED_IMAGE_NAME"
echo "TOTAL BAKE TIME: $((SECONDS - BAKE_START))s"
echo "════════════════════════════════════════════════════════════"
echo "Deploy with: python3 deploy.py --env ${ENV} --use-baked"
