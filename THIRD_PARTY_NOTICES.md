# Third-party components

YayaShare's MIT license applies to its own source, not to bundled dependencies.
The build copies license files from installed dependency distributions to `licenses/`.

- Python: Python Software Foundation license, https://www.python.org/
- Qt / PySide6 / Shiboken6: LGPL/GPL/commercial options as specified by their distribution; https://www.qt.io/ and https://code.qt.io/cgit/pyside/pyside-setup.git/
- cryptography: Apache-2.0 OR BSD-3-Clause, https://github.com/pyca/cryptography
- cffi: MIT, https://foss.heptapod.net/pypy/cffi
- qrcode: BSD, https://github.com/lincolnloop/python-qrcode
- pycparser: BSD-3-Clause, https://github.com/eliben/pycparser
- PyInstaller bootloader: GPL with the distribution exception provided by PyInstaller, https://github.com/pyinstaller/pyinstaller

Qt libraries remain dynamically linked in the onedir application. Users may replace compatible libraries in the bundle and rebuild from this repository with modified dependencies. This application does not prohibit reverse engineering for debugging modifications to LGPL components. The package's licenses directory includes the applicable notices supplied by installed distributions, including Qt component notices where present.
