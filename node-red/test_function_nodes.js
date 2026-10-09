// Tests for the Node-RED function nodes.  Run: node --test node-red/test_function_nodes.js
//
// Each node's source is run the way Node-RED runs a function node body: with
// msg, node, context and env in scope, returning the message (or null).

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

function load(rel) {
    const body = fs.readFileSync(path.join(__dirname, rel), "utf8");
    // eslint-disable-next-line no-new-func
    const fn = new Function("msg", "node", "context", "env", "flow", body);
    const store = {};
    const flowStore = {};
    const warnings = [];
    const statuses = [];
    const node = { warn: (m) => warnings.push(m), error: (m) => warnings.push(m),
                   status: (s) => statuses.push(s) };
    const ctx = { get: (k) => store[k], set: (k, v) => { store[k] = v; } };
    const flow = { get: (k) => flowStore[k], set: (k, v) => { flowStore[k] = v; } };
    const run = (payload, envVars = {}) => fn(
        { payload: (payload && typeof payload === "object") ? { ...payload } : payload },
        node, ctx, { get: (k) => envVars[k] }, flow);
    const send = (msg) => fn({ ...msg }, node, ctx, { get: () => undefined }, flow);
    return { run, send, store, flowStore, warnings, statuses };
}

// ---------------------------------------------------------------- auto-charge

const CHARGER_ONLY = 1, INVERTER_ONLY = 2, ON = 3;

function autoCharge() {
    const n = load("auto-charge/auto-charge.js");
    n.at = (soc, extra = {}) => n.run({
        "Auto Charge": 1, "Charge State": 0, "State of Charge": soc,
        "Multiplus Switch Position Input": ON, ...extra });
    return n;
}

test("auto-charge: below 20% switches to Charger Only", () => {
    assert.strictEqual(autoCharge().at(15).payload, CHARGER_ONLY);
});

test("auto-charge: 20-30% hysteresis holds Charger Only until 30%", () => {
    const n = autoCharge();
    n.at(15);                                                  // drop below 20%
    const at25 = n.at(25, { "Multiplus Switch Position Input": CHARGER_ONLY });
    assert.strictEqual(at25, null);                            // holds Charger Only (no write)
    assert.strictEqual(n.at(30).payload, ON);                  // released at 30%
    assert.strictEqual(n.store.isChargeOnlyActive, false);
});

test("auto-charge: 20-50% without a low-charge event is On", () => {
    assert.strictEqual(autoCharge().at(35).payload, ON);
});

test("auto-charge: >=50%, auto charge on and Float switches to Inverter Only", () => {
    assert.strictEqual(autoCharge().at(60, { "Charge State": 3 }).payload, INVERTER_ONLY);
});

test("auto-charge: >=50% otherwise holds by sending nothing", () => {
    const out = autoCharge().at(60, { "Auto Charge": 0, "Charge State": 3,
                                      "Multiplus Switch Position Input": CHARGER_ONLY });
    assert.strictEqual(out, null);
});

test("auto-charge: missing or invalid SOC holds position (no message)", () => {
    for (const soc of [undefined, null, "55", NaN]) {
        const n = autoCharge();
        assert.strictEqual(n.at(soc), null);
        assert.strictEqual(n.warnings.length, 1);
    }
});

test("auto-charge: a missing or null position is never written to /Mode", () => {
    // Review 2026-10-02: the hold branch echoed Number(position): NaN or 0.
    for (const pos of [undefined, null, "x"]) {
        const n = autoCharge();
        assert.strictEqual(n.at(60, { "Multiplus Switch Position Input": pos }), null);
        assert.match(n.warnings[0], /unknown/);
    }
});

// ---------------------------------------------------------------- low-power-charging

const KEY = "MultiPlus-II 12/3000/120-50 120V - Switch Position";
const VKEY = "MultiPlus-II 12/3000/120-50 120V - Input voltage phase 1";

function lowPower() {
    const n = load("low-power-charging/low-power-charging.js");
    n.at = (volts, extra = {}, envVars = {}) => n.run(
        { "Auto Charge": 1, [KEY]: ON, [VKEY]: volts, ...extra }, envVars);
    return n;
}

test("low-power-charging: auto charge off does nothing", () => {
    assert.strictEqual(lowPower().at(120, { "Auto Charge": 0 }), null);
});

test("low-power-charging: sags drop the limit 1 A at a time to the 9 A floor", () => {
    const n = lowPower();
    n.store.currentLimit = 11;
    assert.strictEqual(n.at(110).payload, 10);
    assert.strictEqual(n.at(110).payload, 9);
    assert.strictEqual(n.at(110).payload, 9);
});

test("low-power-charging: steady voltage climbs 0.1 A without float drift", () => {
    const n = lowPower();
    let out;
    for (let i = 0; i < 100; i++) out = n.at(120);
    assert.strictEqual(out.payload, 19);                       // 9 + 100 x 0.1, exactly
    for (let i = 0; i < 200; i++) out = n.at(120);
    assert.strictEqual(out.payload, 30);                       // capped
});

test("low-power-charging: inside the hysteresis band holds", () => {
    const n = lowPower();
    n.store.currentLimit = 15;
    assert.strictEqual(n.at(116).payload, 15);
});

test("low-power-charging: inverter only resets to the minimum", () => {
    const n = lowPower();
    n.store.currentLimit = 25;
    assert.strictEqual(n.at(120, { [KEY]: INVERTER_ONLY }).payload, 9);
});

test("low-power-charging: missing switch key warns and stops", () => {
    const n = lowPower();
    assert.strictEqual(n.run({ "Auto Charge": 1, [VKEY]: 120 }), null);
    assert.match(n.warnings[0], /Switch Position key/);
});

test("low-power-charging: SWITCH_POSITION_KEY env var selects another inverter", () => {
    const other = "MultiPlus 12/2000/80-50 120V - Switch Position";
    const n = lowPower();
    const out = n.run({ "Auto Charge": 1, [other]: ON, [VKEY]: 120 },
                      { SWITCH_POSITION_KEY: other });
    assert.strictEqual(out.payload, 9.1);
});

// ---------------------------------------------------------------- cfx3-to-virtual

function cfx3() {
    const n = load("../dometic-cfx3/cfx3-to-virtual.js");
    n.topic = (t, p) => n.send({ topic: `dometic/cfx3/${t}`, payload: p });
    return n;
}

test("cfx3: temperatures route to the fridge and freezer outputs", () => {
    const n = cfx3();
    assert.deepStrictEqual(n.topic("fridge/temperature", "2.0"),
                           [{ payload: { Temperature: 2, Status: 0 } }, null]);
    assert.deepStrictEqual(n.topic("freezer/temperature", "-16.5"),
                           [null, { payload: { Temperature: -16.5, Status: 0 } }]);
});

test("cfx3: retained bridge/link in either order never unhide stale temperatures", () => {
    // Review 2026-10-02: one flag shared by both topics, last one wins.
    for (const order of [["link", "bridge"], ["bridge", "link"]]) {
        const n = cfx3();
        const vals = { bridge: "online", link: "disconnected" };
        for (const t of order) n.topic(t, vals[t]);
        assert.deepStrictEqual(n.topic("fridge/temperature", "4.0"),
                               [{ payload: { Temperature: null } }, null], order.join(","));
    }
    for (const order of [["link", "bridge"], ["bridge", "link"]]) {
        const n = cfx3();
        const vals = { bridge: "offline", link: "connected" };
        for (const t of order) n.topic(t, vals[t]);
        assert.deepStrictEqual(n.topic("fridge/temperature", "4.0"),
                               [{ payload: { Temperature: null } }, null], order.join(","));
    }
});

test("cfx3: link down blanks both sensors as Disconnected; back up restores", () => {
    const n = cfx3();
    const out = n.topic("link", "disconnected");
    assert.deepStrictEqual(out, [{ payload: { Temperature: null, Status: 1 } },
                                 { payload: { Temperature: null, Status: 1 } }]);
    assert.strictEqual(n.topic("link", "connected"), null);
    assert.deepStrictEqual(n.topic("fridge/temperature", "3.0"),
                           [{ payload: { Temperature: 3, Status: 0 } }, null]);
});

test("cfx3: fridge off (standby) blanks temperatures but stays Ok", () => {
    const n = cfx3();
    assert.deepStrictEqual(n.topic("power/cooler", "0"),
                           [{ payload: { Temperature: null, Status: 0 } },
                            { payload: { Temperature: null, Status: 0 } }]);
    assert.deepStrictEqual(n.topic("freezer/temperature", "5.0"),
                           [null, { payload: { Temperature: null } }]);
    n.topic("power/cooler", "1");
    assert.deepStrictEqual(n.topic("freezer/temperature", "-15.0"),
                           [null, { payload: { Temperature: -15, Status: 0 } }]);
});

test("cfx3: other topics and garbage produce nothing", () => {
    const n = cfx3();
    assert.strictEqual(n.topic("fridge/setpoint", "2.0"), null);
    assert.strictEqual(n.topic("fridge/temperature", "abc"), null);
    assert.strictEqual(n.send({ payload: "x" }), null);
});

// ---------------------------------------------------------------- outside-temp

test("outside-temp: wraps a reading; garbage produces nothing", () => {
    const n = load("outside-temp/mopeka-to-virtual.js");
    assert.deepStrictEqual(n.run(16.5).payload, { Temperature: 16.5, Status: 0 });
    assert.strictEqual(n.run("not a number"), null);
});

test("outside-temp: the 30 min watchdog blanks the temperature", () => {
    const flow = JSON.parse(fs.readFileSync(path.join(__dirname, "outside-temp/flow.json"), "utf8"));
    const wd = flow.find((x) => x.id === "ot_watchdog");
    assert.deepStrictEqual(JSON.parse(wd.op2), { Temperature: null, Status: 1 });
});

test("flow.json function bodies match their .js sources", () => {
    const pairs = [["outside-temp/flow.json", "ot_wrap", "outside-temp/mopeka-to-virtual.js"],
                   ["../dometic-cfx3/flow.json", "cfx3_route", "../dometic-cfx3/cfx3-to-virtual.js"]];
    for (const [f, id, js] of pairs) {
        const flow = JSON.parse(fs.readFileSync(path.join(__dirname, f), "utf8"));
        assert.strictEqual(flow.find((x) => x.id === id).func,
                           fs.readFileSync(path.join(__dirname, js), "utf8"), `${f} is stale: run build-flow.py`);
    }
});
