# YayaShare 🐾

Share text, clipboard contents, files, and folders between macOS and Windows on the same local network. No accounts, servers, or cloud storage. Built with Python and PySide6 as a small, approachable desktop project.

## Download and get started

Download the ZIP for your platform from [Releases](https://github.com/kiyaya0829/YayaShare/releases/latest):

- **Apple Silicon Mac (including MacBook Air M3):** extract `YayaShare-macOS-arm64.zip` and move `YayaShare.app` into Applications. Requires macOS 13 or later.
- **Windows x64:** extract the entire `YayaShare-Windows-x64.zip` archive and run `YayaShare.exe`. Keep the `_internal` folder beside the executable; do not move the EXE on its own. Windows 11 is recommended.
- Development builds are available under **Actions → Build applications → a successful run → Artifacts**. These downloads require a GitHub account. The artifact ZIP contains the application ZIP.

These builds are not notarized with Apple Developer ID or signed with Windows Authenticode. Your system may report an unverified developer. Check the source and checksums before running the app; there is no need to disable system-wide security protections. Follow your administrator's policies on managed computers.

**The application interface is currently in Chinese.** The steps below describe the corresponding controls in English.

1. Connect both computers to the same Wi-Fi or local network and open YayaShare. Allow access on **private networks**: TCP `45873` handles encrypted transfers, and UDP `45874` handles discovery.
2. On one computer, generate a pairing code and bring the complete code to the other computer through a trusted channel.
3. On the other computer, select the discovered device, open the pairing/manual connection dialog, and paste the code. Pairing is needed only once; both computers save the trust relationship.
4. Select a trusted device and enter text, or read the clipboard into the editor, then send the text.
5. Choose a file or folder to send. Text is limited to 64 KiB; an individual file or an entire folder is limited to 2 GiB. SHA-256 hashes are calculated before streaming begins, and the progress bar updates during transfer.
6. Use the recent items list to view received text and file or folder locations, or open the receive folder. The app never runs received files automatically.

## Features

### Automatic clipboard sync

Upgrade both computers to v1.1 or later and enable **Clipboard Sync** on each. Copy new text on either computer, then paste it on the other.

- Sync is **off at every startup**. Enabling it does not send existing clipboard contents.
- Newly copied text is sent to **all paired devices**. Only text up to 64 KiB is supported; images, file lists, and clipboard data containing file URLs are excluded.
- Automatically synchronized text is not saved to receive history or disk.
- The switch controls both sending and receiving. Turning it off clears queued updates but cannot recall an update already sent over the network.
- The app must stay running. Closing its window exits the app; there is no system tray mode yet. Offline devices produce an error without repeatedly retrying old clipboard contents.
- Sensitive text copied while sync is on is also shared with trusted devices. Enable sync only when you need it.

Network transfers use a separate background task. Qt accesses the system clipboard on the GUI thread, using change notifications and 400 ms polling to detect copies while the app is in the background.

Each update has a unique ID; the latest 512 IDs are tracked for deduplication. Before writing received text, the app updates its local source state, then reads back normalized clipboard text as a baseline to avoid echoes. If both computers copy text simultaneously, each may receive the other's latest update. This version prevents endless back-and-forth syncing but does not guarantee a global ordering of simultaneous updates. It queues at most 64 incoming updates and retains only the latest pending local update.

### Folder transfers

Folder transfers preserve relative paths, Unicode filenames, and empty directories. Each batch is saved in a separate random receive directory.

- Up to 2 GiB total, 2048 entries, 32 path components, and 512 UTF-8 bytes per relative path.
- Incompatible filenames are sanitized; for example, `CON.txt` becomes `_CON.txt`.
- Collisions after sanitization, case folding, or Unicode normalization are rejected with a request to rename the affected entries.
- Absolute paths, parent traversal, symbolic links, and special files are rejected. Windows reports overly long receive paths so you can shorten the hierarchy or data directory.
- Each file is verified before the completed folder appears in history. Interrupted transfers and checksum failures remove the entire temporary batch.

### Application icons

The supplied artwork is used consistently for the window, taskbar/Dock, and application bundles. The complete image and its aspect ratio are preserved, with white padding for square PNG, Windows ICO, and macOS ICNS formats.

## Pairing and connection troubleshooting

A pairing code contains **64 characters**: a certificate fingerprint and a random 128-bit secret. It expires after five minutes and can be used only once. Generating another code immediately replaces the previous one. Discovery messages are not trusted by themselves; the certificate is always checked before sending content.

If the other computer does not appear:

- Allow about 3–12 seconds for discovery. Keep both computers awake and the app open.
- Find the other computer's local IPv4 address in its network settings, such as `192.168.1.20`, and enter its IP and port in the pairing/manual connection dialog.
- If a trusted device changes IP address, select it, enter the new address, and leave the pairing code blank to update its connection details.
- Guest or campus Wi-Fi may isolate devices. Sharing a Wi-Fi name does not guarantee connectivity; try your own router or hotspot.
- Check VPN and firewall settings. On macOS, allow YayaShare under **System Settings → Privacy & Security → Local Network**. Port forwarding is unnecessary; avoid exposing the app to the public internet.
- An “address already in use” error can mean another instance is running. Only one instance may use a given data directory.

## Local data and trust

| Platform | Data directory |
| --- | --- |
| macOS | `~/Library/Application Support/YayaShare` |
| Windows | `%LOCALAPPDATA%\YayaShare` |
| Linux (from source) | `~/.local/share/YayaShare` |

Received files are stored under `Received/<random batch>/<sanitized filename>` inside the data directory. Repeated filenames are stored separately without overwriting earlier files. History keeps the latest 100 items; **removing an item from history does not delete its received files**.

Manual text history, trust tokens, and certificates stay on your computer and are not uploaded to the cloud. Local data is not encrypted at rest, so protect your operating system account and disk.

Revoking a device's trust blocks future requests, although a transfer already in progress may finish. Pair again to reconnect. Deleting the entire data directory resets identity and trust and also deletes received files stored there; back up anything you need first.

## Run from source

Requires Python 3.11 or later. CI builds and tests use Python 3.13.

```bash
git clone https://github.com/kiyaya0829/YayaShare.git
cd YayaShare
python -m venv .venv
```

macOS / Linux:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m yayashare
```

Windows PowerShell (no execution policy changes needed):

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m yayashare
```

Run tests and a brief startup check:

```bash
python -m pytest -q
python -m yayashare --smoke-test --no-discovery --port 0 --data-dir ./build/smoke-data
```

To run two instances on one computer, give each a different `--data-dir` and `--port`, then connect manually through `127.0.0.1`. Automated tests use isolated temporary directories rather than your normal receive folder.

## Project layout

```text
src/yayashare/
  __main__.py     Startup arguments and single-instance lock
  gui.py          Qt Widgets interface and background jobs
  clipboard.py    Session-only clipboard state and loop prevention
  folders.py      Portable folder manifests and streamed transfers
  discovery.py    UDP discovery (not an authentication mechanism)
  security.py     TLS certificates, fingerprint pinning, pairing codes
  protocol.py     JSON framing, trusted authentication, file streaming
  storage.py      Atomic state persistence, history, safe filenames
  assets/        Application artwork and platform icons
tests/           Local two-peer TLS integration, invalid-input, and GUI tests
scripts/         Packaging, icon conversion, and application launcher
.github/workflows/build.yml  Cross-platform tests, builds, and release drafts
```

## Protocol and security design

- Transport uses Python's standard-library `socket`, `ssl`, and `threading` modules. Certificate generation uses `cryptography`; QtNetwork supplies interface broadcast addresses.
- TCP connections establish TLS and verify the pairing-code or stored SHA-256 certificate fingerprint before sending secrets or content.
- Frames consist of a four-byte big-endian length followed by UTF-8 JSON. The wire version remains `1`. Existing `text`, `file`, and `pair` operations are preserved; v1.1 adds `folder` and authenticated `capabilities` requests.
- Automatic clipboard sync checks for `clipboard-text-v1` support before sending a `text` request with `purpose=clipboard`, `mime=text/plain`, and `update_id`. This avoids sending automatic clipboard text to older versions. Manual text and single-file transfers remain compatible with older versions.
- Files use metadata → ready acknowledgment → raw bytes → final acknowledgment. A matching SHA-256 hash is required before success is recorded. Folders send a manifest containing paths, entry types, sizes, and hashes, followed by file bytes in manifest order and a final batch acknowledgment.
- A clipboard acknowledgment means the update entered the in-memory queue. The GUI subsequently writes it to the system clipboard.
- Pairing secrets live briefly in memory, and trust tokens are randomly generated. Only trusted devices may send content. Pairing requests include the initiating device's certificate fingerprint to establish trust in both directions.
- Oversized frames and files are rejected. The service accepts at most eight inbound connections and applies connection, read, and total file-transfer timeouts. Streaming uses fixed-size buffers.
- Single-file paths are reduced to filenames. Sanitization handles Windows special characters, reserved names, control characters, trailing dots/spaces, and UTF-8 byte limits. Receivers use private random directories and exclusively created temporary files, then rename on success and clean up on failure.
- UDP advertisements contain the device name, random ID, certificate fingerprint, and port. Other devices on the local network can see this information. There is no telemetry.
- The app does not yet provide resumable transfers, cancellation, per-transfer approval, encryption at rest, or a professional security audit. Trusted devices can send content while the app is open, so pair only with devices you trust.

## Build and release

```bash
python -m pip install -e '.[dev]'
python scripts/build.py
```

Build on the target operating system: Apple Silicon Python for macOS arm64, or x64 Python for Windows. Output goes to `dist/` and includes an application ZIP and SHA-256 checksum file. PyInstaller's `onedir` layout keeps bundled libraries available for troubleshooting and replacement.

GitHub Actions uses `macos-14` (arm64) and `windows-2022` (x64). It checks the Python architecture, runs tests, packages the app, verifies icon resources, and starts the packaged executable for a smoke test. Test results are saved as JUnit artifacts.

- Push to `main` or run the workflow manually to generate downloadable artifacts.
- Push a `v*` tag to create a **release draft** after both platform builds succeed. The draft includes ZIPs and checksum files; publish it on GitHub after checking the results.

For a new version, update its version metadata, create a matching tag, and push that tag. For example, replace `vX.Y.Z` below with the intended version:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Builds do not include commercial signing certificates. Signing and notarization can be added through repository secrets in a future update.

References: [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) and [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html).

## Possible next steps

- Drag and drop, and multi-file queues
- QR-code pairing
- Transfer cancellation, resume support, receive approval, and disk quotas
- System tray mode, optional launch at login, and image clipboard sync
- mDNS, further multi-interface discovery improvements, IPv6, and device renaming
- Apple notarization, Windows signing, and automatic updates

## License

YayaShare's source code is licensed under the [MIT License](LICENSE). Packaged applications include separately licensed components such as Python, Qt/PySide6, and cryptography. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the bundle's `licenses/` directory.
