# Ensures the repository root is importable when pytest is invoked
# from anywhere (rootdir insertion for `import spatial_catalog`).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
