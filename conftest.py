"""pytest bootstrap (v0.18.2).

Makes plain ``pytest`` work from a fresh clone: the project root is put
on sys.path (tests import top-level modules such as ``orch_ui``) and is
the working directory (some tests read files like ``orch_ui.py`` by
relative path). The unittest runner (``python -m unittest discover -s
tests``) does not need this file.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)
