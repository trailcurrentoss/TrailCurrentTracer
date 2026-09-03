// Blocking WiFi setup.
//
// Without a network this device is worthless — no broker, no Headwaters, no
// discovery, no CAN-over-MQTT. So when `net` reports no association, this
// gate stands in front of the launcher and cannot be dismissed until a
// connection succeeds. It is a precondition, not a settings page.
//
// The network can still be CHANGED later from Settings > Network; this flow
// only handles the cold case of having none at all.
//
// Password entry uses the PHYSICAL keyboard via tracerd's text mode. There is
// no on-screen keyboard anywhere in Tracer.

import { icon } from "../icons.js";
import { rpc, update, state } from "../store/store.js";

export const wifi = {
  stage: "scan",      // scan | list | password | connecting | failed
  networks: [],
  warning: "",
  sel: 0,
  psk: "",
  error: "",
  target: null,
  scanning: false,
};

export async function wifiScan() {
  if (wifi.scanning) return;
  wifi.scanning = true;
  wifi.stage = "scan";
  wifi.error = "";
  update({});
  const res = await rpc("net", "scan");
  wifi.scanning = false;
  if (res.ok) {
    wifi.networks = res.d.networks || [];
    wifi.warning = res.d.warning || "";
    wifi.sel = 0;
    wifi.stage = wifi.networks.length ? "list" : "failed";
    if (!wifi.networks.length) wifi.error = "No networks found";
  } else {
    wifi.stage = "failed";
    wifi.error = (res.err && res.err.msg) || "scan failed";
  }
  update({});
}

async function connect() {
  wifi.stage = "connecting";
  wifi.error = "";
  update({});
  const res = await rpc("net", "connect", {
    ssid: wifi.target.ssid,
    psk: wifi.psk,
  });
  if (res.ok) {
    // Do NOT jump to the launcher here. The gate lifts only when the `net`
    // module itself reports an association — nmcli returning 0 is not proof
    // the link came up.
    wifi.stage = "connecting";
    wifi.error = "";
  } else {
    wifi.stage = "password";
    wifi.error = (res.err && res.err.msg) || "connection failed";
    wifi.psk = "";
    await rpc("input", "set_mode", { mode: "text" });
  }
  update({});
}

// Returns true if it consumed the button.
export async function wifiPress(btn) {
  if (wifi.stage === "list") {
    if (btn === "dpad_up") { wifi.sel = Math.max(0, wifi.sel - 1); update({}); return true; }
    if (btn === "dpad_down") { wifi.sel = Math.min(wifi.networks.length - 1, wifi.sel + 1); update({}); return true; }
    if (btn === "l") { wifi.sel = 0; update({}); return true; }
    if (btn === "r") { wifi.sel = wifi.networks.length - 1; update({}); return true; }
    if (btn === "x") { await wifiScan(); return true; }
    if (btn === "a") {
      const n = wifi.networks[wifi.sel];
      if (!n) return true;
      wifi.target = n;
      wifi.psk = "";
      wifi.error = "";
      if (n.secure && !n.saved) {
        wifi.stage = "password";
        // Hand the keyboard over to text mode so the letter keys type
        // instead of firing A/B/X/Y/L/R.
        await rpc("input", "set_mode", { mode: "text" });
        update({});
      } else {
        await connect();
      }
      return true;
    }
    return true;   // swallow everything else — this screen is a gate
  }

  if (wifi.stage === "password") {
    // B is KEY_B — a letter in the password. start/select are the only
    // buttons available here. Same contract as every other text field.
    if (btn === "a") {
      await rpc("input", "set_mode", { mode: "nav" });
      await connect();
      return true;
    }
    if (btn === "b") {
      wifi.stage = "list";
      wifi.psk = "";
      wifi.error = "";
      await rpc("input", "set_mode", { mode: "nav" });
      update({});
      return true;
    }
    return true;
  }

  if (btn === "x" && (wifi.stage === "failed" || wifi.stage === "list")) {
    await wifiScan();
    return true;
  }
  return true;
}

// Text-mode keystrokes for the password field.
export async function wifiText(ch) {
  if (wifi.stage !== "password") return;
  wifi.psk += ch;
  update({});
}

export async function wifiTextKey(key) {
  if (wifi.stage !== "password") return;
  if (key === "backspace") { wifi.psk = wifi.psk.slice(0, -1); update({}); }
  else if (key === "enter") {
    await rpc("input", "set_mode", { mode: "nav" });
    await connect();
  } else if (key === "escape") {
    // B types a letter in a password field, so Esc is the way out.
    wifi.stage = "list";
    wifi.psk = "";
    wifi.error = "";
    await rpc("input", "set_mode", { mode: "nav" });
    update({});
  }
}

function signalBars(pct) {
  const n = pct >= 75 ? 4 : pct >= 50 ? 3 : pct >= 25 ? 2 : 1;
  let out = "";
  for (let i = 1; i <= 4; i++) {
    const on = i <= n;
    out += `<div style="width:3px;height:${3 + i * 2}px;border-radius:1px;
      background:${on ? "var(--tc-success)" : "var(--fg-off)"};"></div>`;
  }
  return `<div style="display:flex;align-items:flex-end;gap:2px;height:11px;">${out}</div>`;
}

export function wifiScreen() {
  const header = `
    <div style="display:flex;align-items:center;gap:10px;padding:14px 16px 10px;">
      <div style="color:var(--tc-primary);">${icon("wifi", 22)}</div>
      <div>
        <div style="font-size:16px;font-weight:500;">Connect to WiFi</div>
        <div style="font-size:11px;color:var(--fg-mute);">
          Tracer needs a network to reach the vehicle
        </div>
      </div>
    </div>`;

  let body = "";
  let hints = "";

  if (wifi.stage === "scan") {
    body = `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                  justify-content:center;gap:12px;color:var(--fg-mute);">
        <div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;">
          ${icon("sync-outline", 28)}
        </div>
        <div style="font-size:13px;">Scanning…</div>
      </div>`;
  } else if (wifi.stage === "list") {
    const rows = wifi.networks.map((n, i) => {
      const on = i === wifi.sel;
      return `
      <div class="wifi-row" data-idx="${i}"
           style="display:flex;align-items:center;gap:11px;padding:9px 12px;
                  background:${on ? "var(--bg-2)" : "var(--bg-1)"};
                  border:2px solid ${on ? "var(--focus-border)" : "var(--border)"};
                  box-shadow:${on ? "var(--glow)" : "none"};
                  border-radius:10px;">
        ${signalBars(n.signal)}
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;overflow:hidden;text-overflow:ellipsis;
                      white-space:nowrap;">${n.ssid}</div>
          <div style="font-size:10px;color:var(--fg-mute);">
            ${n.security}${n.aps > 1 ? ` · ${n.aps} APs` : ""}${n.saved ? " · saved" : ""}${n.active ? " · connected" : ""}
          </div>
        </div>
        ${n.secure ? `<div style="color:var(--fg-mute);">${icon("checkbox", 13)}</div>` : ""}
      </div>`;
    }).join("");
    const warn = wifi.warning ? `
      <div style="margin:0 16px 6px;padding:6px 10px;border-radius:8px;
                  background:var(--bg-1);border:1px solid var(--tc-warning);
                  color:var(--tc-warning);font-size:10px;line-height:13px;">
        ${wifi.warning}
      </div>` : "";
    body = `${warn}<div style="flex:1;overflow:hidden;padding:0 16px;display:flex;
                        flex-direction:column;gap:6px;">${rows}</div>`;
    hints = "Start  Connect      X  Rescan      Select  Back";
  } else if (wifi.stage === "password") {
    body = `
      <div style="flex:1;padding:0 16px;display:flex;flex-direction:column;gap:10px;">
        <div style="font-size:12px;color:var(--fg-dim);">
          Password for <span style="color:var(--fg);">${wifi.target.ssid}</span>
        </div>
        <div style="padding:11px 12px;background:var(--bg-1);
                    border:2px solid var(--focus-border);box-shadow:var(--glow);
                    border-radius:10px;font-family:var(--mono);font-size:15px;
                    letter-spacing:2px;min-height:42px;">
          ${"•".repeat(wifi.psk.length)}<span
            style="display:inline-block;width:8px;height:15px;
                   background:var(--tc-primary-light);vertical-align:-2px;
                   animation:tcpulse 1s ease-in-out infinite;"></span>
        </div>
        <div style="font-size:11px;color:var(--fg-mute);">
          Enter to connect · Esc to go back · B types a letter here
        </div>
        ${wifi.error ? `<div style="font-size:12px;color:var(--tc-danger);">${wifi.error}</div>` : ""}
      </div>`;
    hints = "Start or Enter  Connect      Select or Esc  Back";
  } else if (wifi.stage === "connecting") {
    body = `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                  justify-content:center;gap:12px;">
        <div style="color:var(--tc-primary);animation:tcspin 1s linear infinite;">
          ${icon("sync-outline", 28)}
        </div>
        <div style="font-size:13px;">Connecting to ${wifi.target ? wifi.target.ssid : ""}…</div>
      </div>`;
  } else {
    body = `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;
                  justify-content:center;gap:12px;">
        <div style="color:var(--tc-danger);">${icon("alert-circle", 28)}</div>
        <div style="font-size:13px;">${wifi.error || "No networks found"}</div>
      </div>`;
    hints = "X Rescan";
  }

  return `
    <div style="position:absolute;inset:0;display:flex;flex-direction:column;
                background:var(--bg-0);">
      ${header}
      ${body}
      <div style="height:30px;display:flex;align-items:center;padding:0 16px;
                  border-top:1px solid var(--border);font-size:11px;
                  color:var(--fg-mute);">${hints}</div>
    </div>`;
}
