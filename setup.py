"""Builds perf's optional C extension, perf._speedups (src/perf/_speedups.c).

Everything else about the package lives in pyproject.toml. The extension is
only attempted on CPython 3.12+ with the GIL (it relies on PyErr_GetRaisedException
and on the GIL for its unlocked state); elsewhere the wheel is pure Python.
`optional=True` turns a failed compile (no C compiler, missing headers) into a
warning, so installing perf never fails because of it -- perf then runs its
pure-Python wrappers, which record exactly the same traces.
"""

import platform
import sys
import sysconfig

from setuptools import Extension, setup

ext_modules = []
if (
    platform.python_implementation() == "CPython"
    and sys.version_info >= (3, 12)
    and not sysconfig.get_config_var("Py_GIL_DISABLED")
):
    ext_modules.append(Extension("perf._speedups", ["src/perf/_speedups.c"], optional=True))

setup(ext_modules=ext_modules)
