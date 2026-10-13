"""Reusable metric cards; widgets are updated in place."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

LEVEL_CLASSES = ("usage-low", "usage-medium", "usage-high")


def _set_text(label: Gtk.Label, text: str) -> None:
    if label.get_label() != text:
        label.set_label(text)


class MetricCard(Gtk.Box):
    """A titled card whose rows are created once and updated in place."""

    def __init__(self, title: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("hw-card")
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)

        heading = Gtk.Label(label=title)
        heading.set_xalign(0)
        heading.add_css_class("hw-title")
        self.append(heading)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.append(self.body)

        self._rows: dict = {}
        self._signature = None

    def reset(self, signature) -> None:
        """Rebuild from scratch only when the card's structure changes."""
        if signature == self._signature:
            return
        child = self.body.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.body.remove(child)
            child = following
        self._rows.clear()
        self._signature = signature

    def _create_row(self, key: str, bar: bool):
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        key_label = Gtk.Label()
        key_label.set_xalign(0)
        key_label.set_hexpand(True)
        key_label.set_wrap(True)
        key_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        key_label.set_max_width_chars(20)
        key_label.add_css_class("hw-key")
        value_label = Gtk.Label()
        value_label.set_xalign(1)
        value_label.set_wrap(True)
        value_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_label.set_max_width_chars(44)
        value_label.add_css_class("hw-value")
        line.append(key_label)
        line.append(value_label)
        container.append(line)

        progress = None
        if bar:
            progress = Gtk.ProgressBar()
            progress.add_css_class("hw-bar")
            container.append(progress)

        self.body.append(container)
        self._rows[key] = (key_label, value_label, progress)
        return self._rows[key]

    def set_row(self, key, label, value, percent=None, level=None, bar=False) -> None:
        entry = self._rows.get(key) or self._create_row(key, bar)
        key_label, value_label, progress = entry
        _set_text(key_label, label)
        _set_text(value_label, str(value))
        if progress is not None:
            fraction = 0.0 if percent is None else max(0.0, min(1.0, percent / 100.0))
            progress.set_fraction(fraction)
            for css_class in LEVEL_CLASSES:
                progress.remove_css_class(css_class)
            if level and percent is not None:
                progress.add_css_class(f"usage-{level}")

