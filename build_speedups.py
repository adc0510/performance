"""Build perf's optional C extension in place (prototype; not yet part of the wheel build).

    python build_speedups.py          # writes src/perf/_speedups<EXT_SUFFIX>

perf works without it (pure-Python wrappers); PERF_PURE_PYTHON=1 ignores a built one.
"""

import subprocess
import sys
import sysconfig
from pathlib import Path

src = Path(__file__).parent / "src" / "perf" / "_speedups.c"
out = src.with_name("_speedups" + sysconfig.get_config_var("EXT_SUFFIX"))
cmd = [
    sysconfig.get_config_var("CC").split()[0] if sysconfig.get_config_var("CC") else "cc",
    "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Wno-missing-field-initializers",
    f"-I{sysconfig.get_paths()['include']}",
    str(src), "-o", str(out),
]
print(" ".join(cmd))
sys.exit(subprocess.call(cmd))
