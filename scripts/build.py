"""Run on the target OS. Produces an onedir application ZIP and SHA-256."""
import hashlib
import importlib.metadata
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    mac = sys.platform == "darwin"
    if sys.platform not in ("darwin", "win32"):
        raise SystemExit("Build on macOS arm64 or Windows x64")
    expected = "arm64" if mac else "AMD64"
    if platform.machine().lower() != expected.lower():
        raise SystemExit(f"Expected {expected} Python, got {platform.machine()}")
    licenses = ROOT / "build/licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for package in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "cryptography", "cffi", "pycparser", "pyinstaller"):
        dist = importlib.metadata.distribution(package)
        for entry in dist.files or []:
            if any(word in str(entry).lower() for word in ("license", "copying", "notice")):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    relative = Path(*[p for p in entry.parts if p not in ("..", ".")])
                    target = licenses / package / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    for filename in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / filename
        if source.exists():
            shutil.copy2(source, licenses / "PYTHON-LICENSE.txt")
            break
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
               "--name", "YayaShare", "--paths", "src", "--add-data", f"{licenses}{os.pathsep}licenses",
               "--add-data", f"THIRD_PARTY_NOTICES.md{os.pathsep}."]
    if mac:
        command += ["--target-architecture", "arm64", "--osx-bundle-identifier", "io.github.kiyaya0829.yayashare"]
    command += ["scripts/launcher.py"]
    subprocess.run(command, check=True)
    if mac:
        plist_path = ROOT / "dist/YayaShare.app/Contents/Info.plist"
        with plist_path.open("rb") as source:
            plist = plistlib.load(source)
        plist.update(CFBundleShortVersionString="0.1.0", CFBundleVersion="0.1.0",
                     NSLocalNetworkUsageDescription="YayaShare 在局域网发现你的电脑，并加密传输你选择的文字和文件。")
        with plist_path.open("wb") as output:
            plistlib.dump(plist, output)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", "dist/YayaShare.app"], check=True)
    executable = ROOT / ("dist/YayaShare.app/Contents/MacOS/YayaShare" if mac else "dist/YayaShare/YayaShare.exe")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    subprocess.run([str(executable), "--smoke-test", "--no-discovery", "--port", "0", "--data-dir", str(ROOT / "build/bundle-smoke")],
                   env=env, check=True, timeout=60)
    name = "YayaShare-macOS-arm64" if mac else "YayaShare-Windows-x64"
    archive = ROOT / "dist" / (name + ".zip")
    if mac:
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", "dist/YayaShare.app", str(archive)], check=True)
    else:
        shutil.make_archive(str(archive.with_suffix("")), "zip", ROOT / "dist", "YayaShare")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Built and smoke-tested: {archive}")


if __name__ == "__main__":
    main()
