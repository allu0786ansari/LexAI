"""Compatibility shim for ragas import issues.

Historically some ragas versions raised import-time errors when certain
langchain components were present in the environment. The original repo
worked around that by importing a small compat stub before importing
ragas. We keep a lightweight no-op stub here so scripts that still
perform `import evaluation._ragas_compat` succeed.

This file intentionally does nothing.
"""

__all__ = []
