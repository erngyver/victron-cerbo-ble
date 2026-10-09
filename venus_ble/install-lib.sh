# Shared deploy steps for the services in this repo. Sourced by each
# component's install.sh (POSIX sh; runs on the client, talks to the GX over ssh).
#
# Everything lands under /data, which survives Venus OS firmware updates.
# /service is tmpfs and is rebuilt on every boot, so the service symlink is
# recreated from /data/rc.local, which S99custom-rc-late.sh runs at startup.

# Run from the component directory whatever the caller's cwd was.
cd "$(dirname "$0")" || exit 2

# Every ssh/scp below shares one connection. An install makes ~20 of them, and
# opening that many back to back over a weak GX Wi-Fi link failed mid-deploy
# (client: "Can't assign requested address"), leaving the service stopped.
# The socket lives under /tmp: macOS caps socket paths at 104 bytes.
SSH_MUX_DIR=$(mktemp -d /tmp/venus-ble.XXXXXX) || exit 2
SSH_MUX="-o ControlMaster=auto -o ControlPath=$SSH_MUX_DIR/%C -o ControlPersist=60 -o ServerAliveInterval=5"
ssh() { command ssh $SSH_MUX "$@"; }
scp() { command scp $SSH_MUX "$@"; }
ssh_mux_close() {
    for s in "$SSH_MUX_DIR"/*; do
        [ -S "$s" ] && command ssh -o ControlPath="$s" -O exit dummy 2>/dev/null
    done
    rm -rf "$SSH_MUX_DIR"
}
trap ssh_mux_close EXIT

# Adapter LE supervision timeout, units of 10 ms, adapter-wide; the Venus
# default of 42 (420 ms) drops links on any brief RF gap. Every installer
# writes this exact line, so it appears once whichever ran last.
SUPERVISION_TIMEOUT_LINE="echo 400 > /sys/kernel/debug/bluetooth/hci0/supervision_timeout 2>/dev/null"

# preflight <host>: the host must answer as root.
preflight() {
    uid=$(ssh -o ConnectTimeout=10 "$1" id -u) || { echo "cannot ssh to $1"; exit 2; }
    [ "$uid" = "0" ] || { echo "$1: must log in as root (Settings > General > Access level)"; exit 2; }
}

# stop_service <host> <link>: stop and wait for the process to exit, so a
# service that restores state on SIGTERM finishes before files are replaced.
stop_service() {
    ssh "$1" "
        [ -e $2 ] || exit 0
        svc -d $2 2>/dev/null || true
        i=0
        while svstat $2 2>/dev/null | grep -q ': up'; do
            i=\$((i + 1)); [ \$i -gt 30 ] && { echo '$2 did not stop within 30s'; break; }
            sleep 1
        done
    "
}

# upload <host> <dest> <files...>: component files, venus_ble and run scripts.
upload() {
    host=$1; dest=$2; shift 2
    ssh "$host" "mkdir -p $dest/service/log && rm -rf $dest/venus_ble && mkdir -p $dest/venus_ble"
    scp "$@" "$host:$dest/"
    # venus_ble/ ships with each service, so updating one never breaks another.
    scp $(ls ../venus_ble/*.py | grep -v /test_) "$host:$dest/venus_ble/"
    scp service/run "$host:$dest/service/run"
    scp service/log/run "$host:$dest/service/log/run"
    ssh "$host" "chmod +x $dest/service/run $dest/service/log/run"
}

# rc_local <host> <line...>: make /data/rc.local contain each line exactly once,
# removing only those exact lines (whole-line, literal) and keeping all others.
rc_local() {
    host=$1; shift
    args=""; adds=""
    for line in "$@"; do
        args="$args -e '$line'"
        adds="$adds '$line'"
    done
    ssh "$host" "
        RC=/data/rc.local
        [ -f \$RC ] || printf '#!/bin/sh\n' > \$RC
        grep -v -x -F $args \$RC > \$RC.new || true
        printf '%s\n' $adds >> \$RC.new
        mv \$RC.new \$RC
        chmod +x \$RC
    "
    for line in "$@"; do
        ssh "$host" "$line"
    done
}

# setting <host> <path>: a localsettings value, or empty if it cannot be read.
setting() {
    ssh "$1" "dbus -y com.victronenergy.settings $2 GetValue 2>/dev/null" | sed 's/value = //' || true
}

# check_bluetooth_service <host>: vesmart-server (Settings > Services >
# Bluetooth) disconnects every BLE device on the adapter 60 s after any
# connection (victronenergy/venus#1587).
check_bluetooth_service() {
    case "$(setting "$1" /Settings/Services/Bluetooth)" in
        0) ;;
        "") echo "NOTE: could not read Settings > Services > Bluetooth; it must be OFF." ;;
        *)  echo
            echo "WARNING: Settings > Services > Bluetooth is ON. VictronConnect-over-Bluetooth"
            echo "         cuts every BLE link after 60 s. Turn it off (BLE sensors can stay on)."
            echo ;;
    esac
}

# start_service <host> <link>
start_service() {
    ssh "$1" "
        svc -u $2 2>/dev/null || { sleep 6; svc -u $2; }
        sleep 2
        svstat $2
    "
}
