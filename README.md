# Wisniowski Connected for Home Assistant

Unofficial Home Assistant custom integration for gates controlled through **Wisniowski Connected** / Lavva cloud.

This integration was built for my own Wisniowski Smart AWSC Wi-Fi gate module and is maintained on a best-effort basis. It is not affiliated with, endorsed by, or supported by WIŚNIOWSKI, Zamel, or Lavva.

## Current Status

Tested with:

- Wisniowski Smart AWSC Wi-Fi / smartAWSC gate module
- Wisniowski Connected Android app `2025.12.05`
- One Lavva/Wisniowski installation
- Home Assistant custom integration setup through OAuth authorization code + PKCE

Not tested:

- Other Wisniowski product types
- Multiple installations on one account
- Other Lavva/OEM brands
- Device pairing, schedules, firmware updates, and advanced app settings

This is a cloud integration. It does not provide offline LAN control. In testing, the gate module connected to Lavva over TLS and rejected a simple local fake broker with an untrusted certificate, so DNS/NAT replacement is not enough for local-only control.

## Features

- UI setup from Home Assistant.
- One-time browser login through the official Lavva Keycloak flow.
- Refresh-token storage and rotation in the Home Assistant config entry.
- Gate discovery from the selected Wisniowski/Lavva installation.
- `cover` entity for each discovered gate.
- Open, close, stop, and set-position commands when the device reports support for direct direction/position control.
- Optional step-by-step button for gate channels that expose the app-style toggle command.
- GraphQL WebSocket subscriptions for push state updates.
- Slow polling fallback every 5 minutes.
- Diagnostic entities for API authentication, gate cloud connection, access-token expiry, refresh-token expiry, and refresh-token rotation time.

## Installation With HACS

For now, add this repository as a HACS custom repository:

1. Open HACS in Home Assistant.
2. Open **Custom repositories**.
3. Add this repository URL.
4. Select category **Integration**.
5. Install **Wisniowski Connected**.
6. Restart Home Assistant.
7. Go to **Settings > Devices & services > Add integration**.
8. Search for **Wisniowski Connected**.

## Manual Installation

Copy this directory:

```text
custom_components/wisniowski_connected
```

to:

```text
<your Home Assistant config>/custom_components/wisniowski_connected
```

Then restart Home Assistant and add **Wisniowski Connected** from **Settings > Devices & services**.

## Configuration

Before adding the integration, your gate must already be paired in the official Wisniowski Connected app. This integration does not pair devices to the account.

Setup flow:

1. Add **Wisniowski Connected** in Home Assistant.
2. Home Assistant shows an authorization URL.
3. Open that URL in a browser.
4. Sign in with your Wisniowski Connected account.
5. The final page may be blank. That is expected.
6. Copy the final URL from the browser address bar. It should contain `code=...`.
7. Paste that final URL back into Home Assistant.

Home Assistant exchanges the authorization code for tokens, discovers the gate channels, and stores the refresh token in the config entry. The account password is not stored by this integration.

## Entities

Each supported gate exposes:

- `cover` entity with open, close, stop, and position support when available.
- `button` entity for step-by-step control if the gate reports that feature.
- `binary_sensor` **Cloud API authenticated**: whether the latest cloud API refresh succeeded with a valid token.
- `binary_sensor` **Gate cloud connection**: whether the cloud reports the module as connected.
- `sensor` **Access token expires**.
- `sensor` **Refresh token expires**.
- `sensor` **Refresh token updated**.

The refresh-token expiry sensor can show `unknown` if the token itself does not contain an expiry claim and Keycloak has not returned `refresh_expires_in` yet. Its attributes explain whether the expiry is known and where the value came from. The integration still rotates refresh tokens when the server provides a new one.

## State Updates

The integration does not poll every few seconds.

Normal operation uses a GraphQL WebSocket subscription, so state changes are pushed from the cloud. A full state refresh is used:

- once during setup/reload,
- every 5 minutes as a fallback,
- 2 seconds after sending a command.

The WebSocket heartbeat is only a connection keepalive, not a full state poll.

## Known Limitations

- Cloud access is required.
- Local/offline control is not implemented.
- Device pairing is not implemented.
- Schedules, firmware updates, account management, and advanced device settings are not implemented.
- Only one real Smart AWSC installation has been tested.
- Other devices may expose different channel types or feature flags.
- HACS and Home Assistant may cache icons. If the icon does not appear immediately, restart Home Assistant and refresh the browser or mobile app.

## Troubleshooting

`invalid_auth`: the authorization code is invalid, expired, already used, or not copied from the final redirect URL. Start the flow again and paste a fresh final URL.

Blank page after login: expected. Copy the full browser address from that page and paste it into Home Assistant.

No gates found: confirm that the account has a paired Wisniowski Connected gate in the official app.

Gate unavailable: check the **Gate cloud connection** diagnostic entity. If it is off, the vendor cloud currently reports the module as disconnected.

Cloud API authenticated is off: check Home Assistant logs for `wisniowski_connected`. The refresh token may have expired or been revoked.

## Privacy And Logs

Do not share account passwords, authorization URLs containing `code=...`, access tokens, refresh tokens, or full unredacted Home Assistant logs.

Useful issue reports include:

- Home Assistant version
- integration version
- gate/module model
- Wisniowski Connected app version
- redacted Home Assistant logs
- screenshots of entity states with sensitive identifiers hidden

## HACS Default Repository Notes

This repository is structured for HACS as a custom integration: one integration under `custom_components/wisniowski_connected`, `hacs.json` in the repository root, and GitHub Actions for HACS and hassfest validation.

For inclusion as a default HACS repository, the project also needs a public GitHub repository with a description, topics, issues enabled, passing HACS/hassfest checks, a GitHub release, and a separate Home Assistant Brands submission for the domain icon/logo.

## Maintenance

This project is maintained in spare time. Pull requests and clear test reports are welcome, but support for untested hardware and account setups cannot be promised.

To run the cloud client regression tests with Python 3.13:

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

The tests use a local HTTP server and synthetic tokens; no Home Assistant installation, cloud account, or gate is required.
