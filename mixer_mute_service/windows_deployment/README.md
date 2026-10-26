# Windows packaging sources

This folder is deliberately separate from the normal application. Development
and local operation remain unchanged:

```bash
python -m app.main
```

On a 64-bit Windows build machine with Python 3.12, create the portable release:

```powershell
.\windows_deployment\build-release.ps1
```

See `DEPLOYMENT-README.md` for installation, updates, configuration and
troubleshooting on the streaming PC.

The release builder:

1. creates an isolated build-only virtual environment;
2. bundles the service and static web UI with PyInstaller in one-folder mode;
3. downloads the pinned stable WinSW 2.12.0 x64 wrapper;
4. verifies the wrapper against its pinned SHA-256 checksum;
5. adds configuration templates and service-management scripts;
6. creates a ZIP ready to copy to the streaming PC.

Generated build environments and release artifacts are ignored by the local
`.gitignore` in this folder.
