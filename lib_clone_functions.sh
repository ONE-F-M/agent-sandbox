# lib_clone_functions.sh — sourced by every 02*_clone_*.sh stage script.
#
# Split out of the original single 02_clone_apps.sh so each clone stage
# (now 4 separate scripts, each its own Docker layer — see the 02a/02b/02c/02d
# scripts and Dockerfile.frappe_runtime's 4 RUN instructions) shares one
# definition of clone_app/clone_plain_repo/_normalize_remote instead of
# duplicating them. The split itself exists to keep any single layer's
# upload small: the original one-RUN-clones-everything design produced a
# single ~6.4GB layer that a dropped connection (Docker Desktop's own
# internal proxy killing long-lived large uploads — confirmed, not a real
# bandwidth problem) would force restarting from zero, over and over, with
# no way to land a full push. Four smaller layers means a drop only costs
# that one layer's retry.
#
# The git buffer/timeout tuning here is `git config --global`, which
# persists in the image's filesystem once set — applying it again in each
# stage script (this file gets sourced by all four) is harmless
# idempotent repetition, not wasted work.

git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999
git config --global core.compression 0

# Call ONLY after pin_app() has already run — pin_app() needs an
# authenticated `git fetch --unshallow` against the private repo (bench's
# clone is shallow, --depth 1) to reach the pinned commit at all, so the
# token-embedded URL must still be in place when it runs. Stripping the
# credential first (as an earlier version of this script did) breaks that
# fetch with "could not read Username for 'https://github.com'" — confirmed
# the hard way on a real bake, failing at onefm_sso's pin step.
#
# Once pinning is done, force the remote to a known-clean state:
#   1. Rename it to "origin" — `bench get-app` names it "upstream", but
#      dev_agent_server.py's _checkout_target_branch() runs `git fetch
#      origin <branch>` for ANY target_app (target_app_choices() offers
#      every installed app, not just the ONE-F-M ones), so every app needs
#      a remote literally named "origin" or that fetch fails outright —
#      this is what broke run DAS-91630 against one_bpmn.
#   2. Overwrite the remote URL with $clean_url (never containing
#      $GITHUB_TOKEN) — `git clone`/`bench get-app` writes whatever URL it
#      was given verbatim into .git/config, so cloning with the token
#      embedded (required for auth against a private repo) leaves the
#      token sitting in plaintext inside this Docker layer forever unless
#      it's overwritten before the RUN instruction finishes. A layer only
#      ever stores the final on-disk state, so doing this inside the same
#      clone_app() call that performed the clone (after pinning) means the
#      token never reaches the committed image.
_normalize_remote() {
    local app_name=$1
    local clean_url=$2
    local app_dir="apps/$app_name"
    local existing
    existing=$(git -C "$app_dir" remote)
    for name in $existing; do
        if [ "$name" != "origin" ]; then
            git -C "$app_dir" remote rename "$name" origin
        fi
    done
    git -C "$app_dir" remote set-url origin "$clean_url"
}

clone_app() {
    local app_name=$1
    local repo_url=$2
    local branch=$3
    # Optional 4th arg: the credential-free URL to leave in .git/config.
    # Defaults to $repo_url for the public frappe-maintained apps, where
    # it's already credential-free; the private ONE-F-M apps pass this
    # explicitly since their $repo_url has $GITHUB_TOKEN embedded for auth.
    local clean_url=${4:-$repo_url}
    local max_retries=3
    local attempt=1

    if [ ! -d "apps/$app_name" ]; then
        echo "Cloning $app_name from $repo_url (branch: $branch)..."
        until [ $attempt -gt $max_retries ]
        do
            echo "Attempt $attempt of $max_retries..."
            rm -rf "apps/$app_name"
            if bench get-app "$app_name" "$repo_url" --branch "$branch" --skip-assets || bench get-app "$repo_url" --branch "$branch" --skip-assets; then
                echo "Successfully cloned $app_name."
                pin_app "$app_name"
                _normalize_remote "$app_name" "$clean_url"
                return 0
            fi
            echo "Failed to clone $app_name. Retrying in 5 seconds..."
            attempt=$((attempt + 1))
            sleep 5
        done
        echo "ERROR: Failed to clone $app_name after $max_retries attempts."
        exit 1
    fi
}

# clone_plain_repo() — for a target_app that isn't a Frappe app at all (no
# hooks.py, never bench-installed): mobile_app_ionic is a standalone Vue/
# Ionic/Capacitor project. `bench get-app` would fail on it (it expects a
# real Frappe app to register), so this does a plain `git clone` into the
# same apps/<name> path instead — _checkout_target_branch/_collect_changed_
# files/_open_pr in dev_agent_server.py all just operate on that directory
# as a git repo, so nothing about them needs to know or care that this one
# was never bench-installed. pin_app/_normalize_remote are the same
# mechanism either way, since they too just operate on the directory.
clone_plain_repo() {
    local app_name=$1
    local repo_url=$2
    local branch=$3
    local clean_url=${4:-$repo_url}
    local max_retries=3
    local attempt=1

    if [ ! -d "apps/$app_name" ]; then
        echo "Cloning $app_name (plain, non-Frappe) from $repo_url (branch: $branch)..."
        until [ $attempt -gt $max_retries ]
        do
            echo "Attempt $attempt of $max_retries..."
            rm -rf "apps/$app_name"
            if git clone --branch "$branch" --single-branch "$repo_url" "apps/$app_name"; then
                echo "Successfully cloned $app_name."
                pin_app "$app_name"
                _normalize_remote "$app_name" "$clean_url"
                return 0
            fi
            echo "Failed to clone $app_name. Retrying in 5 seconds..."
            attempt=$((attempt + 1))
            sleep 5
        done
        echo "ERROR: Failed to clone $app_name after $max_retries attempts."
        exit 1
    fi
}
