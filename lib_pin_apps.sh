# lib_pin_apps.sh — sourced by 01_init_bench.sh and 02_clone_apps.sh.
#
# Kept as its own file, not folded into either stage script, so editing a
# pinned commit invalidates exactly the Docker layers that need to re-clone
# — never the later `bench setup requirements` stage, and never a stage
# script that only changed for reasons unrelated to pinning.
#
# Commits this bench was actually running at capture time (2026-08-24).
# Re-capture these (`git -C apps/<app> rev-parse HEAD` in the real bench)
# whenever you deliberately want the sandbox to track a newer snapshot —
# staleness is a known, accepted tradeoff for reproducibility; see
# bake_image.sh's rebuild cadence note.
declare -A PINNED_SHA=(
    [frappe]="58e17bf6f21b808146b5534def58d8404c411f2d"
    [erpnext]="80de914b55596adf4944c55a566540b48ac66ab7"
    [telephony]="58d32184e44b193e27498d3dd156085c793b7528"
    [helpdesk]="fa7fe76c839716136fc6564ee2b986f3473ef55b"
    [hrms]="4a8094bca52159235c789e73df170cb32477385d"
    [wiki]="2372f859f6b7ee3b0cc7b1515a92cbd6a654e87e"
    [payments]="b2a02d60a14c8ec88687488d5b890bf7a29c81dd"
    [twilio_integration]="cb474c54d794c3a58a8c5287d387cf3d41e5ed0a"
    [lending]="c29f527c0e8ca7eaf95e8d3c74eb502404f2f1ff"
    [one_fm_password_management]="38d67fe2fb6ad925cfa7bdc75215921bdbff2251"
    [one_fm]="c1f8e7e7c07027e82309c4fd5ce14c7a32886dcc"
    [onefm_sso]="26ba468fe4b5262844b5d87f89b24fb8424bc1a9"
    [one_bpmn]="2dd3c7cdeaf3c8f86879810e40e551ab7cd4eec2"
    [onefm_mcp]="f83812cd07bfa3e51966c4d2c5ef94fc9fff6b6a"
    [frappe_agile]="79ca4b3c4f4504aeb1589987b3f441d431727ae9"
)

# Checks out $1's pinned commit. A shallow clone (bench's default) may not
# contain it, so unshallow first — cheap no-op if the clone was already full.
pin_app() {
    local app_name=$1
    local sha=${PINNED_SHA[$app_name]:-}
    if [ -z "$sha" ]; then
        echo "WARNING: no pinned commit recorded for $app_name — leaving at branch tip (drift risk)."
        return 0
    fi
    echo "Pinning $app_name to $sha..."
    git -C "apps/$app_name" fetch --unshallow --quiet 2>/dev/null || git -C "apps/$app_name" fetch --quiet
    if git -C "apps/$app_name" checkout --quiet "$sha"; then
        echo "Pinned $app_name to $sha."
    else
        # A pin can be unreachable if the upstream branch was rebased/
        # force-pushed since the SHA was captured (seen on helpdesk) — that's
        # an upstream history change, not something re-capturing our own SHA
        # fixes. Falling back to branch tip keeps the build unblocked; the
        # tradeoff is this one app can drift and surface an unrelated test
        # failure later, same class of issue pinning exists to prevent.
        echo "WARNING: $sha is not reachable on $app_name's cloned branch (likely rebased upstream since capture) — falling back to branch tip. This app can drift; re-verify if its tests fail for unrelated reasons."
    fi
}
