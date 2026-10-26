"""Small PyInstaller entry point for the Windows distribution.

Keeping this outside ``app`` means the normal development entry point remains
unchanged: ``python -m app.main`` continues to work exactly as before.
"""

from multiprocessing import freeze_support

from app.main import main


if __name__ == "__main__":
    freeze_support()
    main()

