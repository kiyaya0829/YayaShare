# YayaShare 🐾

Share text, screenshots, files, and folders between macOS and Windows over the local network or Tailscale. No accounts, servers, or cloud storage are required by YayaShare.

## Download and get started

Download the ZIP for your platform from [Releases](https://github.com/kiyaya0829/YayaShare/releases/latest):

- **Apple Silicon Mac (including MacBook Air M3):** extract `YayaShare-macOS-arm64.zip` and move `YayaShare.app` into Applications. Requires macOS 13 or later.
- **Windows x64:** extract the entire `YayaShare-Windows-x64.zip` archive and run `YayaShare.exe`. Keep the `_internal` folder beside the executable; do not move the EXE on its own. Windows 11 is recommended.
- Development builds are available under **Actions → Build applications → a successful run → Artifacts**. These downloads require a GitHub account. The artifact ZIP contains the application ZIP.

These builds are not notarized with Apple Developer ID or signed with Windows Authenticode. Your system may report an unverified developer. Check the source and checksums before running the app; there is no need to disable system-wide security protections. Follow your administrator's policies on managed computers.

**The application interface is currently in Chinese.** The steps below describe the corresponding controls in English.

1. Connect both computers to the same Wi-Fi or local network and open YayaShare. Allow access on **private networks**: TCP `45873` handles encrypted transfers, and UDP `45874` handles discovery.
2. On one computer, click **接收六位码配对** (Receive six-digit pairing). It accepts one request within two minutes.
3. On the other computer, select the discovered device or enter its IPv4 address in **配对 / 手动连接**. Leave the code field blank and start pairing. Compare the six digits on both screens and confirm on each computer. For an older release, generate and share a 64-character one-time code through a trusted channel.
4. Select a trusted device and enter text, or read the clipboard into the editor, then send the text.
5. Choose a file or folder, or drop one or more items onto the window with a trusted device selected. Manual text is limited to 64 KiB and a file or folder to 2 GiB. Between updated peers, each file is hashed as it is sent. The progress area lets you cancel a transfer in progress.
6. Use the recent items list to view received text and file or folder locations, or open the receive folder. The app never runs received files automatically.

## Features

### Phone browser portal

Use an iPhone, iPad, or Android browser without installing an app. The desktop app serves the page locally; no hosted website or cloud account is involved.

1. Connect your phone to the same reachable local network.
2. Click **手机网页 · 扫码收发文字和文件** (Phone browser) in the desktop window. Choose the optional HTTPS mode before acknowledging the transport notice, then click **开启手机入口** (Open phone portal).
3. Choose the computer's Wi-Fi IPv4 address if multiple addresses appear. Scan the QR code with the phone camera and open it in Safari or your browser. You can also copy the temporary link. Treat the complete link as a temporary password and do not forward it.
4. On the phone, enter text and tap **发送文字**, or choose photos/files and tap **发送选中文件**. Wait for the message confirming receipt on the computer. Received items appear in the desktop history and ordinary receive directory.
5. To send back to the phone, explicitly add text or a file in the desktop portal window. It appears under **从电脑接收** on the phone. Copy the text or download the file. On iPhone, downloads can be found in the browser's download list / Files app; photos are not automatically saved to the Photos library.
6. Keep the desktop portal window open while using it. Closing it, clicking the revoke button, or reaching 30 minutes invalidates the link and browser session. Reopen and scan a new code to reconnect.

The link authorizes **one browser session** and can be used only once. Refreshing that same browser page retains access through its temporary cookie. Another browser, private browsing session, or another phone needs a new portal. Desktop pairing codes and the phone QR code are separate.

- Phone text: up to 64 KiB. Files: up to **256 MiB each**, sent sequentially when selecting multiple files. Each session allows up to 50 incoming items / 512 MiB and 50 outgoing items / 512 MiB of file snapshots. No folder uploads or resumable transfers yet.
- The page can only download files/text you explicitly share in this window. Existing history, folders, desktop trust tokens, and other local files are not exposed. Outgoing files are copied into a temporary snapshot; closing the portal removes these copies but preserves originals and received files.
- Keep both devices awake and the browser in the foreground during transfers. Cancelling interrupts the current upload; files already received remain on the computer. After a network error, check desktop history before retrying to avoid duplicates.
- Browser clipboard access varies: the Copy button falls back to selecting text for manual copying. Automatic background clipboard sync is available only between desktop apps. The app does not convert photos or guarantee their original format; the browser chooses the uploaded representation.
- Allow **TCP 45875** on the computer's trusted/private network while the portal is open. Campus/guest Wi-Fi client isolation can block even a correct QR link. Use a trusted router or personal hotspot when necessary; scanning cannot bypass network isolation. Do not forward this port to the internet.

HTTP is the default for the phone portal and sends content and cookies unencrypted. HTTPS generates a temporary self-signed certificate with the listed IPv4 addresses and displays its SHA-256 fingerprint. Export the public certificate from the dialog and transfer it to the phone through a trusted channel. On iPhone or iPad, export the configuration profile, install it and enable full TLS trust before scanning the QR code. Remove the profile after using the portal. The phone must trust the certificate and treat the page as a secure context to use the modern clipboard API. [Apple certificate trust instructions](https://support.apple.com/en-ie/102390)

### Automatic clipboard sync

Enable **Clipboard Sync** on both computers. To sync screenshots and clipboard pictures, also enable **同步截图和剪贴板图片** on both.

- Sync is off on first launch and the app remembers your choices. Enabling it takes the current clipboard as its baseline.
- New copies go to all paired devices. Updated peers support 512 KiB of UTF-8 text or a 16 MiB PNG, with a 24-megapixel decoded image limit. Older clipboard-capable peers receive up to 64 KiB of text. Clipboard file URLs are excluded.
- Synced contents stay in memory and are omitted from receive history.
- The switch controls both sending and receiving. Turning it off clears queued updates but cannot recall an update already sent over the network.
- Closing the window hides the app when a tray icon is available. Use the tray menu to reopen it or quit. Launch with `--minimized` to start in the tray, including from a login shortcut. Launching a second copy opens the existing window for that data directory.
- Sensitive text copied while sync is on is also shared with trusted devices. Enable sync only when you need it.

Network transfers use a separate background task. Qt accesses the system clipboard on the GUI thread, using change notifications and 400 ms polling to detect copies while the app is in the background.

The app deduplicates by update ID and reads back received clipboard contents as a baseline after writing them. For pictures it baselines the SHA-256 of the encoded PNG. The incoming queue holds up to 64 updates, including at most one image.

### Folder transfers

Folder transfers preserve relative paths, Unicode filenames, and empty directories. Completed folders appear directly in the receive directory.

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
- If a trusted device changes IP address, select it, enter the new address, and leave the pairing code blank to update its connection details. With Tailscale running on both computers, peers exchange their Tailscale IPv4 addresses during pairing or authenticated capability exchange. The app tries any recently discovered LAN address first and then falls back to Tailscale. For a first pairing outside the LAN, enter the other computer's Tailscale IPv4 manually.
- Guest or campus Wi-Fi may isolate devices. Sharing a Wi-Fi name does not guarantee connectivity; try your own router or hotspot.
- Check VPN and firewall settings. On macOS, allow YayaShare under **System Settings → Privacy & Security → Local Network**. Port forwarding is unnecessary; avoid exposing the app to the public internet.
- An “address already in use” error can mean another instance is running. Only one instance may use a given data directory.

## Local data and trust

| Platform | Data directory |
| --- | --- |
| macOS | `~/Library/Application Support/YayaShare` |
| Windows | `%LOCALAPPDATA%\YayaShare` |
| Linux (from source) | `~/.local/share/YayaShare` |

Desktop files and folders are published directly under `Received` after verification. Use **更改接收目录** to pick another location. Duplicate names get a suffix such as `report (2).pdf`; existing history continues to point at the original paths. Phone uploads use separate receive batches under the selected directory. History keeps the latest 100 items.

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
  web_portal.py   Opt-in local HTTP server, temporary authorization, file quotas
  web_dialog.py   Desktop QR code and explicit phone sharing controls
  web/           Self-contained mobile HTML, CSS, and JavaScript
  assets/        Application artwork and platform icons
tests/           Local two-peer TLS integration, invalid-input, and GUI tests
scripts/         Packaging, icon conversion, and application launcher
.github/workflows/build.yml  Cross-platform tests, builds, and release drafts
```

## Protocol and security design

- Desktop transport uses Python's standard-library `socket`, `ssl`, and `threading` modules. Certificate generation uses `cryptography`; QtNetwork supplies interface broadcast addresses. The optional phone portal uses the standard-library HTTP server and `qrcode`; its distinct security model is described above.
- TCP connections establish TLS and verify the pairing-code or stored SHA-256 certificate fingerprint before sending secrets or content.
- Frames consist of a four-byte big-endian length followed by UTF-8 JSON. The wire version remains `1`. Existing `text`, `file`, and `pair` operations are preserved; v1.1 adds `folder` and authenticated `capabilities` requests.
- Authenticated capability negotiation can continue into a transfer on the same pinned TLS connection. Short-lived capability caching skips repeat probes. `clipboard-text-v2` and `clipboard-image-v1` use bounded binary bodies and SHA-256. `stream-v2` provides a digest trailer per file and falls back to prehashed transfers for older peers.
- Files use metadata → ready acknowledgment → raw bytes → final acknowledgment. A matching SHA-256 hash is required before success is recorded. Folders send a manifest containing paths, entry types, sizes, and hashes, followed by file bytes in manifest order and a final batch acknowledgment.
- A clipboard acknowledgment means the update entered the in-memory queue. The GUI subsequently writes it to the system clipboard.
- Desktop pairing secrets live briefly in memory, and trust tokens are randomly generated. Only trusted desktop devices may send through the desktop protocol. Pairing requests include the initiating device's certificate fingerprint to establish trust in both directions. The phone portal uses a separate temporary browser authorization.
- Oversized frames and files are rejected. The service accepts at most eight inbound connections and applies connection, read, and total file-transfer timeouts. Streaming uses fixed-size buffers.
- Single-file paths are reduced to filenames. Sanitization handles Windows special characters, reserved names, control characters, trailing dots/spaces, and UTF-8 byte limits. Receivers use private random directories and exclusively created temporary files, then rename on success and clean up on failure.
- UDP advertisements contain the device name, random ID, certificate fingerprint, and port. Other devices on the local network can see this information. There is no telemetry.
- The app does not yet provide resumable transfers, per-transfer approval, encryption at rest, or a professional security audit. Trusted desktop devices can send content while the app is open, so pair only with devices you trust.

## Build and release

```bash
python -m pip install -e '.[dev]'
python scripts/build.py
```

Build on the target operating system: Apple Silicon Python for macOS arm64, or x64 Python for Windows. Output goes to `dist/` and includes an application ZIP and SHA-256 checksum file. PyInstaller's `onedir` layout keeps bundled libraries available for troubleshooting and replacement.

GitHub Actions uses `macos-15` (arm64) and `windows-2022` (x64). It checks the Python architecture, runs tests, packages the app, verifies icon resources, and starts the packaged executable for a smoke test, including QR generation and loading the bundled phone page. Test results are saved as JUnit artifacts.

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
- QR-code pairing between desktops and native mobile support
- Transfer cancellation, resume support, receive approval, and disk quotas
- A built-in launch-at-login setting
- mDNS, further multi-interface discovery improvements, IPv6, and device renaming
- Apple notarization, Windows signing, and automatic updates

## License

YayaShare's source code is licensed under the [MIT License](LICENSE). Packaged applications include separately licensed components such as Python, Qt/PySide6, and cryptography. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the bundle's `licenses/` directory.
