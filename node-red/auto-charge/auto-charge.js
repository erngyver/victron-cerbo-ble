// Constants
const CHARGE_THRESHOLD = 50; // Threshold for State of Charge
const LOW_CHARGE_THRESHOLD = 20; // Low threshold for State of Charge
const CHARGE_ONLY_THRESHOLD = 30; // Threshold to switch from Charge Only to ON
const OUTPUT_CHARGE_ONLY = 1; // Multiplus Switch Position: Charger Only
const OUTPUT_ON = 3; // Multiplus Switch Position: ON
const OUTPUT_INVERTER_ONLY = 2; // Multiplus Switch Position: Inverter Only

// Text values for output states
const TEXT_VALUES = {
    1: "Charger Only",
    3: "On",
    2: "Inverter Only"
};

// Inputs (from the joined message payload)
const autoCharge = msg.payload["Auto Charge"]; // Auto Charge input (1 = ON, 0 = OFF)
const chargeState = msg.payload["Charge State"]; // Charge State input (0-9)
const stateOfCharge = msg.payload["State of Charge"]; // State of Charge input (0-100)
const currentSwitchPosition = msg.payload["Multiplus Switch Position Input"]; // Current switch position

// Validate State of Charge before acting on it. A missing value coerces to 0 in
// JS comparisons and would force Charger Only, so bail out and hold position instead.
if (typeof stateOfCharge !== 'number' || isNaN(stateOfCharge)) {
    node.warn("Invalid or missing State of Charge: " + stateOfCharge + " - holding current position");
    return null;
}

// The current position is only echoed back when holding; it must be a real
// /Mode value (1-4), or the hold branch would write NaN or 0 to the MultiPlus.
const positionKnown = [1, 2, 3, 4].includes(currentSwitchPosition);

// Context variables to track state
const isChargeOnlyActive = context.get('isChargeOnlyActive') || false;

// Logic
let outputValue = null; // Initialize output value
let debugCondition = ""; // Track which condition was triggered

if (stateOfCharge < LOW_CHARGE_THRESHOLD) {
    // State of Charge is below 20%, set to Charger Only
    outputValue = OUTPUT_CHARGE_ONLY;
    debugCondition = "SOC < 20% - Charger Only";
    context.set('isChargeOnlyActive', true); // Mark Charge Only as active
} else if (stateOfCharge >= CHARGE_ONLY_THRESHOLD && isChargeOnlyActive) {
    // State of Charge has reached 30% and was previously in Charge Only, set to ON
    outputValue = OUTPUT_ON;
    debugCondition = "SOC >= 30% & was Charge Only - Switch to ON";
    context.set('isChargeOnlyActive', false); // Reset Charge Only state
} else if (stateOfCharge >= LOW_CHARGE_THRESHOLD && stateOfCharge < CHARGE_THRESHOLD && !isChargeOnlyActive) {
    // State of Charge is between 20% and 50% and we are not recovering from a
    // low-charge event, set to ON. The !isChargeOnlyActive guard preserves the
    // 20%->30% hysteresis: once we drop below 20% we stay in Charger Only until
    // SOC climbs back to 30% (handled by the condition above).
    outputValue = OUTPUT_ON;
    debugCondition = "SOC between 20-50% - ON";
} else if (stateOfCharge >= CHARGE_THRESHOLD && autoCharge === 1 && chargeState === 3) {
    // State of Charge is >= 50%, Auto Charge is ON, and Charge State is Float (3)
    // Set output to Inverter Only to stop charging
    outputValue = OUTPUT_INVERTER_ONLY;
    debugCondition = "SOC >= 50%, AutoCharge ON, Charge State Float (3) - Inverter Only";
} else {
    // Conditions not met for any action (e.g. SOC >= 50% but Auto Charge off or
    // not in Float, or recovering between 20 and 30%): keep the current
    // position by sending nothing, rather than rewriting /Mode every message.
    if (!positionKnown) {
        node.warn("Holding, but the current switch position is unknown: " + currentSwitchPosition);
    }
    return null;
}

// Construct the output message
msg.payload = Number(outputValue); // Ensure payload is a number
msg.topic = "Multiplus Switch Position Output";
msg.textvalue = TEXT_VALUES[outputValue] ?? `Unknown (${outputValue})`;

// Add debug information
msg.debug = {
    inputs: {
        autoCharge: autoCharge,
        chargeState: chargeState,
        stateOfCharge: stateOfCharge,
        currentSwitchPosition: currentSwitchPosition
    },
    context: {
        isChargeOnlyActive: isChargeOnlyActive,
        newIsChargeOnlyActive: context.get('isChargeOnlyActive')
    },
    condition: debugCondition,
    output: {
        value: outputValue,
        text: msg.textvalue
    }
};

// Return the modified message
return msg;