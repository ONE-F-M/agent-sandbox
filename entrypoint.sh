#!/bin/bash
# entrypoint.sh
# Forked from onefm-ai-agent's entrypoint.single.sh — same MariaDB/Redis boot
# sequence and site-regeneration logic, extended install-app loop (all 15 apps
# this bench runs, in dependency order), and a different default tail: instead
# of falling back to `bench start`, the container's default process is the
# generic, config-free dev_agent_server.py driver (see that file — it carries
# no baked-in agent prompt/tools of its own; those arrive per-request from
# Processa). Explicit commands still work for local debugging, e.g.:
#   docker run ... bench --site SITE run-tests --app one_bpmn

set -e
[ -f /env ] && set -a && . /env && set +a

export HF_HOME="/home/frappe/.cache"
export HOME="/home/frappe"

rm -f /home/frappe/.local/bin/bench

# --- Configuration Defaults ---
BENCH_DIR="/home/frappe/frappe-bench"
SITE_NAME="${SITE_NAME:-sandbox}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"

# Apps installed onto SITE_NAME, in dependency order (erpnext before
# hrms/lending; telephony before helpdesk; one_bpmn before onefm_mcp;
# one_fm last — it depends on erpnext). See each app's hooks.py
# required_apps for the source of this ordering.
INSTALL_APPS=(
  erpnext hrms lending telephony helpdesk payments wiki twilio_integration
  one_fm_password_management onefm_sso frappe_agile one_bpmn onefm_mcp one_fm
)

# Start Redis
service redis-server start

echo "DEBUG: Waiting for Redis to be ready on port 6379..."
for i in {1..5}; do
  if exec 3<>/dev/tcp/127.0.0.1/6379; then
    exec 3<&-
    exec 3>&-
    echo "DEBUG: Redis is ready."
    break
  fi
  echo "DEBUG: Redis not ready yet, waiting..."
  sleep 0.5
done

# --- MariaDB Startup Logic ---
if [ "$SKIP_SETUP" = "1" ]; then
    echo "DEBUG: Fast Boot enabled (SKIP_SETUP=1). Starting MariaDB normally..."
    pkill -9 mysqld || true
    rm -f /run/mysqld/mysqld.sock || true
    mysqld_safe --datadir=/var/lib/mysql &
else
    echo "DEBUG: Cleaning up any stale MariaDB processes..."
    pkill -9 mysqld || true
    sleep 2

    echo "DEBUG: Forcing MariaDB root password (mysql_native_password) to '$MYSQL_ROOT_PASSWORD' on startup."
    mysqld_safe --skip-grant-tables --datadir=/var/lib/mysql &
    for i in {1..15}; do
      [ -S /run/mysqld/mysqld.sock ] && break
      echo 'MariaDB socket not ready (skip-grant-tables), waiting...'
      sleep 1
    done
    sleep 1
    set +e
    mysql -u root --skip-password -e "FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}'; FLUSH PRIVILEGES;"
    if [ $? -ne 0 ]; then
      echo "DEBUG: Modern ALTER USER syntax failed, trying legacy SET PASSWORD syntax."
      mysql -u root --skip-password -e "FLUSH PRIVILEGES; SET PASSWORD FOR 'root'@'localhost' = PASSWORD('${MYSQL_ROOT_PASSWORD}'); FLUSH PRIVILEGES;"
    fi
    set -e
    kill $(pgrep -f 'mysqld.*--skip-grant-tables')
    sleep 2

    echo "DEBUG: Starting MariaDB normally after password/plugin fixup..."
    mysqld_safe --datadir=/var/lib/mysql &
fi

for i in {1..15}; do
  [ -S /run/mysqld/mysqld.sock ] && break
  echo 'MariaDB socket not ready, waiting...'
  sleep 1
done

echo "DEBUG: Testing root login..."
if mysql -u root -p"${MYSQL_ROOT_PASSWORD}" -e "SELECT 1;"; then
  echo "DEBUG: Root login with password succeeded."
else
  echo "DEBUG: Root login with password failed."
  exit 1
fi

trap 'echo "Shutting down MariaDB and Redis..."; mysqladmin -uroot -p"$MYSQL_ROOT_PASSWORD" shutdown; service redis-server stop; exit 0' SIGTERM SIGINT

# --- Fix duplicate entries in one_fm's patches.txt (known upstream issue) ---
PATCHES_FILE="$BENCH_DIR/apps/one_fm/one_fm/patches.txt"
if [ -f "$PATCHES_FILE" ]; then
    ORIG_LINES=$(wc -l < "$PATCHES_FILE")
    awk '!seen[$0]++' "$PATCHES_FILE" > "${PATCHES_FILE}.deduped" && \
        mv "${PATCHES_FILE}.deduped" "$PATCHES_FILE"
    NEW_LINES=$(wc -l < "$PATCHES_FILE")
    if [ "$ORIG_LINES" != "$NEW_LINES" ]; then
        echo "DEBUG: Fixed patches.txt — removed $((ORIG_LINES - NEW_LINES)) duplicate line(s)."
    fi
fi

check_site_exists() {
  [ -f "$BENCH_DIR/sites/$SITE_NAME/site_config.json" ]
}

create_and_install_site() {
    echo "DEBUG: Creating new site '$SITE_NAME'..."
    su - frappe -c "rm -rf $BENCH_DIR/sites/$SITE_NAME $BENCH_DIR/sites/currentsite.txt"
    su - frappe -c "cd $BENCH_DIR && bench new-site $SITE_NAME \
      --admin-password $ADMIN_PASSWORD \
      --mariadb-root-password $MYSQL_ROOT_PASSWORD \
      --install-app frappe --force"

    for app in "${INSTALL_APPS[@]}"; do
        echo "DEBUG: Installing $app to '$SITE_NAME'..."
        su - frappe -c "cd $BENCH_DIR && bench --site $SITE_NAME install-app $app" || \
            echo "WARNING: $app install failed — continuing with remaining apps."
    done

    echo "DEBUG: Running initial migration..."
    su - frappe -c "cd $BENCH_DIR && bench --site $SITE_NAME migrate" || true

    # Without this, `bench run-tests` prints "Testing is disabled for the
    # site!" and exits 0 without running a single test — every dispatch was
    # silently reporting tests_passed regardless of what actually changed,
    # confirmed via a real run whose stdout_tail showed exactly that message.
    echo "DEBUG: Enabling tests on the sandboxed site..."
    su - frappe -c "cd $BENCH_DIR && bench --site $SITE_NAME set-config allow_tests true"

    echo "DEBUG: Site creation complete."
}

if [ "$SKIP_SETUP" = "1" ]; then
    echo "DEBUG: Fast Boot enabled (SKIP_SETUP=1). Checking if baked site exists..."
    if ! check_site_exists; then
      echo "DEBUG: SKIP_SETUP=1 but site '$SITE_NAME' is missing. Creating site now..."
      create_and_install_site
    else
      echo "DEBUG: Baked site '$SITE_NAME' found. Skipping setup."
    fi
else
    APPS_TXT="$BENCH_DIR/sites/$SITE_NAME/apps.txt"
    APPS_DIR="$BENCH_DIR/apps"
    if [ -f "$APPS_TXT" ]; then
      while read -r app; do
        if [ ! -d "$APPS_DIR/$app" ]; then
          echo "FATAL: App '$app' listed in $APPS_TXT is missing from $APPS_DIR."
          exit 1
        fi
      done < "$APPS_TXT"
    fi

    if ! check_site_exists; then
      create_and_install_site
    fi

    echo "DEBUG: Running pre-startup migration to ensure module map integrity."
    su - frappe -c "cd $BENCH_DIR && bench --site $SITE_NAME clear-cache"
    su - frappe -c "cd $BENCH_DIR && bench --site $SITE_NAME clear-website-cache"

    if su - frappe -c "cd $BENCH_DIR && bench --site $SITE_NAME migrate --skip-failing"; then
      echo "DEBUG: Bench migration successful. Environment is stable."
    else
      echo "WARNING: Bench migration had errors but --skip-failing allowed startup to continue."
    fi
fi

if [ ! -f "$BENCH_DIR/sites/currentsite.txt" ]; then
    echo "$SITE_NAME" > "$BENCH_DIR/sites/currentsite.txt"
    chown frappe:frappe "$BENCH_DIR/sites/currentsite.txt"
fi

# --- Hand off ---
# Explicit command: run it as the frappe user (local debugging / manual runs).
# No command: always start the generic driver — never `bench start`. The
# driver carries zero baked-in agent configuration; it waits for a job
# payload (system prompt, tools, skills, target app, branch) over HTTP from
# Processa. See dev_agent_server.py.
if [ $# -gt 0 ]; then
  echo "DEBUG: Executing custom command as frappe: $@"
  exec sudo -E -H -u frappe bash -c "$*"
else
  echo "DEBUG: Starting dev_agent_server.py (generic, config-free driver)..."
  exec sudo -E -H -u frappe bash -c "cd /home/frappe && python3 dev_agent_server.py"
fi
