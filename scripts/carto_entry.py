"""PyInstaller entry point.

A frozen build has no console-script machinery, so the ``carto`` entry point
declared in ``engine/pyproject.toml`` is restated here as the one module
PyInstaller is pointed at.
"""

import multiprocessing

from cartograph.cli import main

if __name__ == "__main__":
    # Parallel parsing spawns workers by re-executing this binary (macOS,
    # Windows); without this each worker runs the CLI instead of its task.
    multiprocessing.freeze_support()
    main()
