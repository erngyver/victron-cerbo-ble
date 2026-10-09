#!/bin/sh
# Deploy the soft BMS to a Cerbo GX.
#
#   ./install.sh <ssh-host> <config.json>     start from config.example.json
#
# The config is validated with the service's own loader before anything is
# touched on the GX. Stopping the running bridge restores the charge-limit
# baseline (it does so on SIGTERM), so the install never leaves a limit behind.

set -e
HOST="$1"
[ -n "$HOST" ] && [ -f "$2" ] || { echo "usage: $0 <ssh-host> <config.json>"; exit 2; }
CONFIG="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"     # absolute: the library cds
. "$(dirname "$0")/../venus_ble/install-lib.sh"
python3 -c "import sys; sys.path.insert(0, '.'); import config; config.load_config(sys.argv[1])" "$CONFIG" \
    || { echo "invalid config: $CONFIG"; exit 2; }

DEST=/data/softbms
LINK=/service/softbms

echo "Deploying $(basename "$CONFIG") to ${HOST}:${DEST}"
preflight "$HOST"
stop_service "$HOST" "$LINK"
upload "$HOST" "$DEST" bridge.py chargelimit.py config.py controller.py readings.py \
       redodo.py roypow.py probe.py
scp "$CONFIG" "$HOST:${DEST}/config.json"
rc_local "$HOST" "$SUPERVISION_TIMEOUT_LINE" "[ -e ${LINK} ] || ln -s ${DEST}/service ${LINK}"
check_bluetooth_service "$HOST"
[ "$(setting "$HOST" /Settings/Services/Bol)" = "1" ] \
    || echo "NOTE: DVCC is not on (Settings > DVCC); the charge limit has no effect without it."
start_service "$HOST" "$LINK"

echo
echo "Done. Follow the log with:"
echo "  ssh ${HOST} 'tail -f /var/log/softbms/current | tai64nlocal'"
