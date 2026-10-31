# PyInstaller specification for the portable Windows folder.
# Run via build-release.ps1 from the mixer_mute_service project directory.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_dir = Path.cwd()
deployment_dir = project_dir / "windows_deployment"

# Uvicorn selects its HTTP, event-loop and lifespan implementations through
# dynamic imports, so explicitly collect them for the frozen build.
hidden_imports = collect_submodules("uvicorn") + collect_submodules("websockets")

a = Analysis(
    [str(deployment_dir / "launcher.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(project_dir / "app" / "static"), "app/static"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "httpx"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MixerMuteService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MixerMuteService",
)

