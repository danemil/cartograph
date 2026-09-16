"""PyInstaller entry point.

A frozen build has no console-script machinery, so the ``carto`` entry point
declared in ``engine/pyproject.toml`` is restated here as the one module
PyInstaller is pointed at.
"""

from cartograph.cli import main

if __name__ == "__main__":
    main()
