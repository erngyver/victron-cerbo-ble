// Input: msg.payload contains the input voltage as a number
// Output: msg.payload contains the adjusted input current limit (A)

// Configurable constants
const MIN_CURRENT_LIMIT = 9;  // Minimum input current limit (A) Can't set this any lower and have it actually do anything
const MAX_CURRENT_LIMIT = 30; // Maximum input current limit (A)
const MIN_VOLTAGE = 115;      // Minimum acceptable voltage (V)
const VOLTAGE_HYSTERESIS = 2; // Voltage hysteresis to avoid rapid changes (V)
// Device-specific payload key: "<product name> - Switch Position" as joined
// upstream. Override per node with the function node's environment variable
// SWITCH_POSITION_KEY (Setup tab) rather than editing the code.
const SWITCH_POSITION_KEY = env.get("SWITCH_POSITION_KEY")
    || "MultiPlus-II 12/3000/120-50 120V - Switch Position";

// Check if Auto Charge is enabled
const autoCharge = msg.payload["Auto Charge"]; // Auto Charge input (1 = ON, 0 = OFF)
if (autoCharge !== 1) {
    // If Auto Charge is not enabled, do nothing
    return null;
}

// Check the MultiPlus switch position
const switchPosition = msg.payload[SWITCH_POSITION_KEY];
if (switchPosition === undefined) {
    node.warn("Switch Position key '" + SWITCH_POSITION_KEY + "' missing in payload.");
    return null;
}
if (![1, 3].includes(switchPosition)) {
    // If not in Charger Only (1) or On (3), set current limit to minimum and exit
    context.set('currentLimit', MIN_CURRENT_LIMIT);
    msg.payload = MIN_CURRENT_LIMIT;
    return msg;
}

// Extract the input voltage from the message
const inputVoltageKey = Object.keys(msg.payload).find(key => key.includes("Input voltage phase 1"));
if (!inputVoltageKey) {
    node.warn("Input Voltage key is missing in the payload.");
    return null; // Stop processing
}

const inputVoltage = msg.payload[inputVoltageKey]; // Current input voltage
let currentLimit = context.get('currentLimit') || MIN_CURRENT_LIMIT; // Default to min current limit

// Validate the input voltage
if (typeof inputVoltage !== 'number' || inputVoltage <= 0) {
    // If the input voltage is invalid, log a warning and do nothing
    node.warn("Invalid input voltage: " + inputVoltage);
    return null; // Stop processing
}

// Adjust the input current limit based on the voltage
if (inputVoltage < MIN_VOLTAGE) {
    // Reduce the current limit to prevent voltage drop
    currentLimit = Math.max(MIN_CURRENT_LIMIT, currentLimit - 1); // Decrease by 1A, minimum MIN_CURRENT_LIMIT
} else if (inputVoltage > MIN_VOLTAGE + VOLTAGE_HYSTERESIS) {
    // Increase the current limit if voltage is stable. Round to 1 decimal to avoid
    // floating point accumulation (e.g. 9.999999998) over many increments.
    currentLimit = Math.min(MAX_CURRENT_LIMIT, Math.round((currentLimit + 0.1) * 10) / 10); // Increase by 0.1A, maximum MAX_CURRENT_LIMIT
}

// Save the updated current limit in the context
context.set('currentLimit', currentLimit);

// Output the new current limit value
msg.payload = currentLimit;
return msg;