#!/bin/sh
# Deploy the CFX3 bridge to a Cerbo GX.
#
#   ./install.sh <ssh-host> <config.json>     start from config.example.json
#
# config.json holds the fridge's Bluetooth address. After installing, pair once
# (see README) and import flow.json in Node-RED.

set -e
HOST="$1"
[ -n "$HOST" ] && [ -f "$2" ] || { echo "usage: $0 <ssh-host> <config.json>"; exit 2; }
CONFIG="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"     # absolute: the library cds
. "$(dirname "$0")/../venus_ble/install-lib.sh"
python3 -c "
import json, re, sys
c = json.load(open(sys.argv[1]))
assert re.fullmatch(r'([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', c.get('mac', '')), 'mac'
assert c.get('address_type', 'public') in ('public', 'random'), 'address_type'
" "$CONFIG" || { echo "invalid config: $CONFIG"; exit 2; }

DEST=/data/dometic
LINK=/service/dometic-cfx3

echo "Deploying to ${HOST}:${DEST}"
preflight "$HOST"
stop_service "$HOST" "$LINK"
upload "$HOST" "$DEST" bridge.py probe.py
scp "$CONFIG" "$HOST:${DEST}/config.json"
rc_local "$HOST" "$SUPERVISION_TIMEOUT_LINE" "[ -e ${LINK} ] || ln -s ${DEST}/service ${LINK}"
check_bluetooth_service "$HOST"
start_service "$HOST" "$LINK"

echo
echo "Done. Follow the log with:"
echo "  ssh ${HOST} 'tail -f /var/log/dometic-cfx3/current | tai64nlocal'"
