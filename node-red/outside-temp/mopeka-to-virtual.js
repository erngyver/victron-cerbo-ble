// Mopeka Temperature -> Victron Virtual Outdoor Sensor
// Wraps the tank sensor's temperature for a victron-virtual temperature node.
// Input:  {"payload": 16.0}                      (from victron-input node, °C)
// Output: {"payload": {"Temperature": 16.0, "Status": 0}}
//
// The Mopeka sits under the propane tank outside, so its temperature is a
// usable outdoor reading that Venus otherwise only shows as tank data.
// Status follows the Victron temperature enum: 0 = Ok, 1 = Disconnected.
// A separate trigger node blanks the temperature (null, shown as "--") and
// sets Status 1 if nothing arrives for 30 minutes.

const STATUS_OK = 0;

try {
    const temperature = parseFloat(msg.payload);

    if (isNaN(temperature)) {
        node.warn("Could not parse temperature from: " + msg.payload);
        node.status({fill: "red", shape: "dot", text: "Parse error"});
        return null;
    }

    node.status({fill: "green", shape: "dot", text: temperature.toFixed(1) + "°C"});
    msg.payload = {Temperature: temperature, Status: STATUS_OK};
    return msg;

} catch (error) {
    node.error("Error wrapping Mopeka temperature: " + error.message);
    node.status({fill: "red", shape: "dot", text: "Error"});
    return null;
}
