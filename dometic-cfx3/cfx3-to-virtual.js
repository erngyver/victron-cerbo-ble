// CFX3 MQTT -> Victron Virtual Temperature Sensors
// Routes bridge telemetry onto two victron-virtual nodes, one per zone.
// Input:  {"topic": "dometic/cfx3/fridge/temperature", "payload": "2.0"}
// Output: [{"payload": {"Temperature": 2.0, "Status": 0}}, null]   (fridge)
//         [null, {"payload": {"Temperature": -16.0, "Status": 0}}] (freezer)
//
// When there is nothing trustworthy to show, the temperature is published as
// null, which the victron-virtual node writes to D-Bus as invalid. That is
// what a real Venus sensor does when it loses contact, and it is the only
// thing the GX display and VRM actually render (as "--"): a Status flag on
// its own changes nothing visible, and a stale number looks healthy.
//
// Two conditions hide the temperatures:
//   - the BLE link is down or the bridge process is dead (Status 1)
//   - the fridge is switched off. A CFX3 "off" is standby: it keeps its
//     Bluetooth up and keeps reporting the temperature of a warming box, so
//     without this VRM shows +17C in the freezer as a valid reading.
//
// Status follows the Victron temperature enum: 0 = Ok, 1 = Disconnected.

const STATUS_OK = 0;
const STATUS_DISCONNECTED = 1;

try {
    if (!msg.topic || typeof msg.topic !== 'string') {
        node.warn("Message without a topic");
        return null;
    }

    const suffix = msg.topic.replace(/^dometic\/cfx3\//, '');
    const payload = String(msg.payload).trim();

    // "bridge" (process alive, last will) and "link" (BLE up) are separate
    // retained topics delivered in no fixed order after a Node-RED restart,
    // so each has its own flag: one coming back must not clear the other.
    const hidden = () => flow.get('cfx3_bridge_down') || flow.get('cfx3_link_down')
        || flow.get('cfx3_cooler_off');
    const blank = (status) => {
        const out = {payload: {Temperature: null, Status: status}};
        return [out, {payload: Object.assign({}, out.payload)}];
    };

    // Bridge process died (MQTT last will), or the BLE link dropped.
    if (suffix === 'bridge' || suffix === 'link') {
        const down = (payload === 'offline' || payload === 'disconnected');
        flow.set(suffix === 'bridge' ? 'cfx3_bridge_down' : 'cfx3_link_down', down);
        if (down) {
            node.status({fill: "red", shape: "ring", text: suffix + ": " + payload});
            return blank(STATUS_DISCONNECTED);
        }
        node.status({fill: "green", shape: "dot", text: suffix + ": " + payload});
        return null;    // the next temperature restores the sensors
    }

    // Fridge master power. 0 is standby: electronics on, cooling off.
    if (suffix === 'power/cooler') {
        const off = (payload === '0');
        flow.set('cfx3_cooler_off', off);
        if (off) {
            node.status({fill: "grey", shape: "ring", text: "fridge off"});
            return blank(STATUS_OK);
        }
        node.status({fill: "green", shape: "dot", text: "fridge on"});
        return null;
    }

    if (suffix !== 'fridge/temperature' && suffix !== 'freezer/temperature') {
        // Setpoints, doors, battery, history: published for other flows.
        return null;
    }

    const isFridge = (suffix === 'fridge/temperature');

    // Retained temperatures can arrive after an "off" on restart; keep them out.
    if (hidden()) {
        const out = {payload: {Temperature: null}};
        return isFridge ? [out, null] : [null, out];
    }

    const temperature = parseFloat(payload);
    if (isNaN(temperature)) {
        node.warn("Could not parse temperature from: " + msg.payload);
        node.status({fill: "red", shape: "dot", text: "Parse error"});
        return null;
    }

    node.status({
        fill: "green",
        shape: "dot",
        text: (isFridge ? "fridge " : "freezer ") + temperature.toFixed(1) + "°C"
    });
    const out = {payload: {Temperature: temperature, Status: STATUS_OK}};
    return isFridge ? [out, null] : [null, out];

} catch (error) {
    node.error("Error routing CFX3 telemetry: " + error.message);
    node.status({fill: "red", shape: "dot", text: "Error"});
    return null;
}
