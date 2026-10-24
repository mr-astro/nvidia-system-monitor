"""Compact, wrapping GTK4 SMART section; only consumes prepared metrics."""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango

from src.gui.storage_smart_model import prepare_smart
from src.gui.widgets import _set_text


SMART_CSS = """
.storage-smart { margin-top: 6px; padding: 8px 10px; border-radius: 8px;
                 background-color: alpha(currentColor, 0.035); }
.storage-smart-heading { font-size: 85%; font-weight: 700; }
.storage-smart-state { font-size: 80%; font-weight: 600; }
.storage-smart-caption { font-size: 80%; opacity: 0.75; }
.storage-smart-value { font-size: 95%; font-weight: 600; }
.storage-smart-note { font-size: 80%; }
.storage-smart-warning { font-size: 85%; }
.storage-smart-diagnostic { font-size: 85%; }
.storage-smart-stale .storage-smart-value,
.storage-smart-unknown .storage-smart-value { opacity: 0.65; }
.storage-smart-failed { font-weight: 700; }
"""


def wrapping_label(css):
    widget = Gtk.Label(xalign=0)
    widget.set_hexpand(True)
    widget.set_wrap(True)
    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_ellipsize(Pango.EllipsizeMode.NONE)
    widget.set_max_width_chars(48)
    widget.add_css_class(css)
    return widget


class SmartSection(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.add_css_class('storage-smart')
        self.set_hexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.heading = wrapping_label('storage-smart-heading')
        self.heading.set_label('SMART')
        self.status = wrapping_label('storage-smart-state')
        self.status.set_xalign(1)
        header.append(self.heading)
        header.append(self.status)
        self.append(header)
        self.metrics = Gtk.FlowBox()
        self.metrics.set_selection_mode(Gtk.SelectionMode.NONE)
        self.metrics.set_min_children_per_line(1)
        self.metrics.set_max_children_per_line(3)
        self.metrics.set_homogeneous(True)
        self.metrics.set_column_spacing(12)
        self.metrics.set_row_spacing(4)
        self.values = {}
        for key, title in (('health', 'Salud'), ('wear', 'Desgaste'), ('hours', 'Horas')):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            column.set_valign(Gtk.Align.START)
            caption = wrapping_label('storage-smart-caption')
            caption.set_label(title)
            value = wrapping_label('storage-smart-value')
            value.set_label('N/D')
            value.set_max_width_chars(22)
            column.append(caption)
            column.append(value)
            self.metrics.insert(column, -1)
            self.values[key] = value
        self.append(self.metrics)
        self.freshness = wrapping_label('storage-smart-caption')
        self.error = wrapping_label('storage-smart-diagnostic')
        self.warnings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.warnings_heading = wrapping_label('storage-smart-heading')
        self.warnings = wrapping_label('storage-smart-warning')
        self.warnings_box.append(self.warnings_heading)
        self.warnings_box.append(self.warnings)
        for child in (self.freshness, self.error, self.warnings_box):
            self.append(child)
        self.error.set_visible(False)
        self.warnings_box.set_visible(False)
        self.view = None

    def update(self, reading, now):
        view = prepare_smart(reading, now)
        self.view = view
        _set_text(self.status, view.status)
        self.status.set_tooltip_text(view.detail)
        self.set_tooltip_text(view.detail)
        for key, widget in self.values.items():
            _set_text(widget, getattr(view, key))
            widget.set_tooltip_text(view.detail)
        _set_text(self.freshness, view.freshness)
        _set_text(self.error, view.error or '')
        self.error.set_visible(bool(view.error))
        _set_text(self.warnings_heading, view.warnings_title)
        _set_text(self.warnings, '\n'.join(view.warnings))
        self.warnings_box.set_visible(bool(view.warnings))
        for state in ('valid', 'unknown', 'stale', 'error'):
            css = 'storage-smart-' + state
            if state == view.state:
                self.add_css_class(css)
            else:
                self.remove_css_class(css)
        # Health failure belongs to the health value, never the warning count.
        if view.failed:
            self.values['health'].add_css_class('storage-smart-failed')
        else:
            self.values['health'].remove_css_class('storage-smart-failed')
