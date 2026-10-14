#!/usr/bin/env python3
"""Entry point: python3 bin/system_monitor.py"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.utils.channel import parse_channel_args


def main(argv=None):
    try:
        channel, gtk_argv = parse_channel_args(sys.argv if argv is None else argv)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 6):
            raise ValueError("se requiere GTK >= 4.6")
    except (ImportError, ValueError) as exc:
        sys.stderr.write(
            f"No se pudo cargar GTK >= 4.6 ({exc}).\n"
            "En Fedora: sudo dnf install gtk4 python3-gobject\n"
        )
        return 1

    from src.gui.app import main as run_app
    return run_app(channel=channel.name, argv=gtk_argv)


if __name__ == "__main__":
    raise SystemExit(main())
