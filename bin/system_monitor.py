#!/usr/bin/env python3
"""Entry point: python3 bin/system_monitor.py"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import gi

    gi.require_version("Gtk", "4.0")
except (ImportError, ValueError) as exc:
    sys.stderr.write(
        f"No se pudo cargar GTK 4 ({exc}).\n"
        "En Fedora: sudo dnf install gtk4 python3-gobject\n"
    )
    raise SystemExit(1)

from src.gui.app import main  # noqa: E402

raise SystemExit(main())
