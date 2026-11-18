import os

EXCLUDES = [
    "setuptools", "pkg_resources", "_distutils_hack", "distutils", "packaging",
    "cryptography", "OpenSSL", "cffi", "_cffi_backend",
    "chardet",
    "brotli", "_brotli", "brotlicffi", "zstandard",
    "asyncio", "multiprocessing", "concurrent",
    "xmlrpc", "pydoc", "pydoc_data", "unittest", "lib2to3", "test", "doctest",
    "tkinter.test", "idlelib", "turtle", "turtledemo",
]

SKIP_DATA = ("_tcl_data\\tzdata", "_tk_data\\demos", "_tk_data\\images", "_tcl_data\\msgs", "_tk_data\\msgs")

a = Analysis(
    ["wifi_login_gui.py"],
    pathex=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=2,
)
a.datas = [d for d in a.datas if not d[0].startswith(SKIP_DATA)]
a.binaries = [b for b in a.binaries if not os.path.basename(b[0]).lower().startswith(("_brotli", "_cffi_backend"))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WiFiAutoLogin",
    console=False,
    upx=False,
    uac_admin=os.environ.get("WIFI_NO_UAC") != "1",
    runtime_tmpdir=None,
)
