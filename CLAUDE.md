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
- Long-term goal: HA decides when to charge, on a bi-hourly (day/night) tariff.

## Current scope (since 0.3.0): read-only + Start/Stop

The maintainer asked to start small: the integration **must not change any setting
on the charger**. Allowed traffic: pairing, reads, read requests (bytes `02 50` config,
`02 40` clock read) and the two buttons, Start charging (`01 00`,
charger's own mode) and Stop charging (`06 00`), only when pressed. No mode, current
or clock writes, and nothing automatic. `test_connecting_never_writes_to_the_charger`
guards this. Smart charging, the mode select, the current numbers and clock sync
were removed in 0.3.0 (see git history before that version to bring them back).
`client.set_config` and `sync_time` are kept, unused, for later.
Don't add any write without the maintainer asking for it.

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
4. The legacy 15-byte config shows the off-peak schedule as start == stop for the
   weekend (0–0 UTC, shown 01:00 - 01:00). Probably "whole day", unconfirmed. The
   maintainer doesn't care (HA will drive charging), so it's low priority.
5. Not yet verified on the real charger: that the Start/Stop buttons start and stop
   a session (Start uses 0x01 0x00, the charger's own mode).
6. 0.3.0 confirmed in the field: the connection holds, drops now and then (weak
   signal) and **reconnects by itself**. 0.2.x no longer reconnected; keep the
   read-only connect sequence (no clock/config write on connect).
7. Diagnostics from the real charger (0.3.0): model 60211, firmware 2.53.2,
   hardware A2, mode code 5 (Max open). Legacy config
   `1410050600ec042c01000000007262`: week 1260–300, weekend 0–0 (minutes, UTC;
   ESPHome adds a fixed +1 h, we use HA's time zone).
   **No CBOR config.** The newer CBOR config read (`04 50`, answer `0x5006`) never
   worked on this charger, and retrying it on every connection (0.3.1) made the
   connection much worse: short bursts, then unavailable for hours (this firmware
   likely drops the link on it). Its only extra values were 4 installer settings
   (peak consumption limit, min charging speed, phase count, connection type). The
   maintainer asked to remove it completely: since 0.3.4 there is no CBOR code at
   all. Don't bring it back; the legacy config has everything needed.
8. Every notification was delivered 2–5 times: after re-pairing (BlueZ
   `RemoveDevice`) bleak keeps the old connection's device watcher, which keeps
   forwarding notifications. `client._notification` drops anything that doesn't
   come through the current `BleakClient`.
9. Field log (0.3.1): "is already paired" → link dropped ~2.5 s later → "Access
   check failed: Characteristic ...db was not found!", repeated every few minutes.
   BlueZ kept a bond the charger had forgotten, so encryption failed and the charger
   dropped the link; the generic error path reused the same bond forever. Since
   0.3.2, a link lost during setup after reusing an existing pairing raises
   `AuthenticationRequired`, so the coordinator unpairs and pairs again
   (`test_link_dropped_with_reused_pairing_pairs_again`).
   Field log (Nov 28) still showed "already paired" → drop → plain "Connection
   failed", no re-pairing: the reuse detection relied only on `async_get_state`
   (queried before connecting). Since 0.3.5 `pairing.async_pair` returns False
   when BlueZ already had a pairing, and that alone marks the pairing as reused
   (`test_reused_pairing_detected_by_the_pairing_step`).

## Architecture

| File | Role |
|---|---|
| `protocol.py` | Pure byte encoding/decoding (CRC-16/MODBUS, legacy 15-byte config, data characteristics). **No HA imports.** |
| `pairing.py` | BlueZ D-Bus pairing agent (`org.bluez.Agent1`, KeyboardOnly). **No `from __future__ import annotations`**: dbus-fast reads the D-Bus signatures from the annotations. Error classification: `InvalidPasskey` (only if the agent was asked for the PIN), `PairingInterrupted`, `PasskeyNotRequested`, `AuthenticationRequired`. |
| `client.py` | One BLE connection: notifications, polling, request/ready/write/success handshakes on the generic command/status/data characteristics. `connected` = link up AND setup finished (`_ready`). |
| `coordinator.py` | Reconnect loop with back-off, re-pair on `AuthenticationRequired`, reauth only on a real PIN rejection, device-registry update (`async_get_or_create`; don't use the deprecated `async_get_device`). |
| `__init__.py` | Platforms, and removal of entities from older versions (`REMOVED_ENTITIES`: 0.2.x writes, 0.3.x CBOR sensors) and of the `cbor_supported` flag 0.3.3 stored. |
| `diagnostics.py` | Raw payloads, config and schedule minutes, last ~40 protocol exchanges, errors (PIN/serial redacted). The first thing to ask the user for when debugging. |

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install pytest-homeassistant-custom-component
pytest            # protocol, D-Bus pairing (needs `dbus-daemon`), HA integration with a simulated charger
```

- `tests/test_integration.py::FakeCharger` simulates the charger's handshakes. Add a
  flag there to reproduce each new field bug before fixing it (see
  `encrypted`, `sends_time_success`, `drop_during_subscribe`).
- Keep `strings.json`, `translations/en.json` and `translations/fr.json` in sync. Every
  `translation_key` needs an entry in all three.
- Bump `version` in `manifest.json` for each release the user installs. They install
  by copying `custom_components/nexxtender_ble` into `/config/custom_components/`
  (or via HACS from this repo) and restarting HA.
- HACS installs from `main`: once tests pass, push each fix to `main` as well
  (fast-forward, no force-push). The maintainer has approved this standing rule.
  Bumping `version` in `manifest.json` on `main` makes `.github/workflows/release.yml`
  publish a `vX.Y.Z` GitHub release; HACS only offers updates through releases.

## Open items

- Confirm on the real charger that Start/Stop work and that the read values match
  the Nexxtmove app.
- Off-peak schedule decoding (finding 4); needs a diagnostics dump from the user.
- Later, only when the maintainer asks: bring back writes one at a time (mode,
  smart charging with the off-peak calendars and public holidays, current
  modulation). Mind flash wear: every config write goes to the charger's flash.
  Never rewrite a setting in the background.
