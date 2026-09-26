"""Mock legacy core-banking app ("CU Member Servicing").

A deliberately hostile stand-in for a real back-office system: server-rendered,
table layouts, an iframe shell, non-semantic markup, no ids / test ids, and
switchable runtime faults. Exists so discovery and replay can be exercised
against realistic errors without touching any real institution's software.
"""
from .app import create_app

__all__ = ["create_app"]
