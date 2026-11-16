# CLAUDE.md: Nexxtender BLE (Home Assistant custom integration)

Context for Claude Code sessions working on this repo. The maintainer writes in French.
Answer in French unless they switch language.

## What this is

A Home Assistant custom integration (`custom_components/nexxtender_ble`) that talks
to a **Powerdale Nexxtender Home** EV charger directly over Bluetooth LE, through the
Bluetooth adapter of the HA host. It doesn't use an ESP32, MQTT or the cloud. The
protocol comes from community reverse-engineering:
[geertmeersman/nexxtender](https://github.com/geertmeersman/nexxtender) (ESPHome,
the reference implementation; check its `config/nexxtender_packages/*.yaml` when in
doubt) and [toSvenson/nexxtender-ble](https://github.com/toSvenson/nexxtender-ble).

## Test setup (field environment)

- HA OS in a VM with the host's Bluetooth adapter passed through over USB. HA is in
  **French**, so entity IDs are generated from the French names (see
  `translations/fr.json`).
- The charger advertises **without a name** (not "HOME"). It's picked by MAC address
  in the config flow.
- The signal is weak, so disconnects happen.
- Goal: HA decides when to charge, on a bi-hourly (day/night) tariff.

## Field findings (real charger); don't regress these

1. Passkey pairing works through our own BlueZ agent (`pairing.py`). HA's Bluetooth
   stack can only do "Just Works" pairing.
2. The charger answers **GATT error 0x0E "Unlikely Error"** to any read or write on an
   unencrypted link, and it doesn't seem to keep a lasting bond. BlueZ doesn't treat
   0x0E as "please encrypt". So every connection: check access
   (`client._verify_access`). If refused, remove the BlueZ device (`async_unpair`)
   and pair again with the PIN. The ESPHome client also calls `pair()` on every
   connect. A reconnect takes 15–30 s; that's expected.
3. The charger **never sends 0x4002 "time set success"**. Clock sync treats it as
   optional and verifies by reading the clock back (0x4003), like ESPHome.
4. The legacy 15-byte config shows the off-peak schedule as start == stop
   (23:00 - 23:00 after UTC→local). It's unresolved whether the CBOR config holds
   the real values. CBOR is preferred when available. The user doesn't care (HA
   drives charging), so it's low priority.
5. Not yet verified on the real charger: that "start charging (max)" (0x02 0x00)
   starts a session in **Max private** mode. Smart charging depends on it.

## Architecture

| File | Role |
|---|---|
| `protocol.py` | Pure byte encoding/decoding (CRC-16/MODBUS, legacy config, CBOR config, data characteristics). **No HA imports.** |
| `pairing.py` | BlueZ D-Bus pairing agent (`org.bluez.Agent1`, KeyboardOnly). **No `from __future__ import annotations`**: dbus-fast reads the D-Bus signatures from the annotations. Error classification: `InvalidPasskey` (only if the agent was asked for the PIN), `PairingInterrupted`, `PasskeyNotRequested`, `AuthenticationRequired`. |
| `client.py` | One BLE connection: notifications, polling, request/ready/write/success handshakes on the generic command/status/data characteristics. `connected` = link up AND setup finished (`_ready`). |
| `coordinator.py` | Reconnect loop with back-off, re-pair on `AuthenticationRequired`, reauth only on a real PIN rejection, daily clock sync, device-registry update (`async_get_or_create`; don't use the deprecated `async_get_device`). |
| `smart_charging.py` + `tariff.py` | Off-peak charging: charger forced to Max private; start when plugged in and off-peak (or "Charge now"); stop when off-peak ends; at most 3 start attempts (full battery). Calendars: Brussels/Flanders (22–7 + weekend), Wallonia 2026 (22–7 + 11–17 daily), shifted 21–6 + weekend. Public holidays aren't handled. |
| `diagnostics.py` | Raw payloads, CBOR values, schedules in both formats, last ~40 protocol exchanges, errors (PIN/serial redacted). The first thing to ask the user for when debugging. |

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install pytest-homeassistant-custom-component
pytest            # protocol, tariff, D-Bus pairing (needs `dbus-daemon`), HA integration with a simulated charger
```

- `tests/test_integration.py::FakeCharger` simulates the charger's handshakes. Add a
  flag there to reproduce each new field bug before fixing it (see
  `encrypted`, `sends_time_success`, `drop_during_subscribe`).
- Keep `strings.json`, `translations/en.json` and `translations/fr.json` in sync. Every
  `translation_key` needs an entry in all three.
- Bump `version` in `manifest.json` for each release the user installs. They install
  by copying `custom_components/nexxtender_ble` into `/config/custom_components/`
  (or via HACS from this repo) and restarting HA.

## Open items

- Confirm on the real charger that smart charging starts and stops sessions in Max private.
- Off-peak schedule decoding (finding 4); needs a diagnostics dump from the user.
- Public holidays (the HA `holiday` integration could feed this).
- Maybe: modulate the charging current instead of start/stop. Mind flash wear: every
  config write goes to the charger's flash.
