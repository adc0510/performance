"""Tiny multi-module package the cases import from (cross-module calls,
re-exports, inheritance across files, registries, pool workers)."""

from .util import normalize  # re-exported: `from app import normalize`
