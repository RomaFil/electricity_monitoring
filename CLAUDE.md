# Svitlo-Local — notes for Claude sessions

Roman's own light monitoring: a NodeMCU (ESP8266) in the village pings Healthchecks; a FastAPI server on
a VPS (reachable only through WireGuard, 10.0.0.1:3003) polls Healthchecks and power-guard on the home
Proxmox laptop; dashboard in `server/app/static/index.html`. Overview and setup: README.md (Ukrainian).

Talk to Roman in Ukrainian. Code, comments and commit messages in English.

## State (24.10.2026)

Remote control is set up and tested on the board in the village (`docs/remote-setup.md`, parts 1–4 done):
signing keys made on Roman's PC (`firmware/private.key`, git-ignored; `firmware/public.key` committed),
the command key is in the VPS `.env` (`ESP_CMD_KEY`), on the board and in `firmware/secrets.ini` (`[cmd] key`).
Sensor re-read, reboot and a remote update 0.7.0 -> 0.7.1 from GitHub Releases all passed, without a false
outage. A new firmware: bump `FW_VERSION`, build on the PC with `private.key`, release `v<version>` with
`firmware.bin.signed` attached under exactly that name, then the dashboard button (the server re-reads the
release list every 10 minutes).

## Hard rules

- `firmware/private.key` never leaves Roman's PC: never commit it, print it, upload it or paste it anywhere.
  If it already exists, never run `make_keys.py` again (a new key locks out the board with the old public.key).
- The command key (`ESP_CMD_KEY`) goes only to the VPS `.env` and the board's settings. Not into git, not into
  the firmware (it is public on GitHub Releases). Show it to Roman once so he can keep it in KeePass.
- Never factory-reset the board and never send `/reset`.
- `POST /settings` on the board: since 0.7.0 fields that are not sent keep their values. On 0.6.x a partial
  POST wipes the Wi-Fi networks and the URL — there, only use the full form in a browser.
- First flash of 0.7.0: USB (`--upload-port COM16`) or `-e ota` over the village Wi-Fi. Remote OTA only works
  once 0.7.0 with `public.key` is on the board.
- A new firmware goes to the village only after it ran on the board while Roman is next to it: ESP8266 has no
  rollback, a broken Wi-Fi or command path means a trip to the village.
- Do not open ports anywhere (router, VPS). Everything is outgoing-only by design.
- Builds with `-DSVITLO_TEST_CRASH` / `-DSVITLO_TEST_NO_WIFI` exist only to test the return to the golden copy
  (`golden.cpp`): use a throw-away version like `0.8.0.1`, upload locally, never attach them to a release.
- Do not add a lazily allocated BearSSL object (trust anchors, keys) in the middle of a run: HTTPS needs one free
  ~16.7 KB block and the heap is ~27 KB (see README "Пам'ять і HTTPS"); use `trust.cpp` / allocate in setup().

## Commands

```
cd tests && python -m unittest                      # server + power-guard tests (FastAPI tests skip without it)
cd firmware && python -m platformio run             # build; with private.key also makes firmware.bin.signed
python -m platformio run -t upload --upload-port COM16
python -m platformio run -e ota -t upload           # over Wi-Fi via /update; needs firmware/secrets.ini
python tools/lcd_preview.py                         # LCD pictures in docs/lcd/ (after display.cpp changes)
```

`firmware/secrets.ini` (git-ignored) holds the board address and admin password (`[ota] host/password`).

## Where things are

- Firmware: `firmware/src/` — `remote.cpp` (commands from the Healthchecks check description, HMAC check),
  `ota.cpp` (signature check, download from GitHub at boot), `web.cpp` (board pages), `display.cpp` (LCD).
- Signing: `firmware/signing.py` (PlatformIO extra script), `firmware/make_keys.py`.
- Server: `server/app/main.py` (`/api/esp/command`, `/api/esp/firmware`), `healthchecks.py` (`sign_cmd`),
  `firmware.py` (GitHub releases with `firmware.bin.signed`).
- Command HMAC is pinned by `tests/test_healthchecks.py::test_signature_matches_the_firmware`: if it fails,
  the board would reject every command.
