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
        self._sections: dict = {}
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
        self._sections.clear()
        self._signature = signature

    def _section(self, name, detail=False):
        if name not in self._sections:
            if detail:
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                expander = Gtk.Expander(label="Detalles técnicos")
                expander.add_css_class("metric-details")
                expander.set_child(content)
                self.body.append(expander)
            else:
                content = Gtk.FlowBox()
                content.set_selection_mode(Gtk.SelectionMode.NONE)
                content.set_min_children_per_line(1)
                content.set_max_children_per_line(2)
                content.set_homogeneous(True)
                content.set_column_spacing(12)
                content.set_row_spacing(6)
                content.add_css_class("metric-highlights")
                self.body.append(content)
            self._sections[name] = expander if detail else content
        widget = self._sections[name]
        return widget.get_child() if detail else widget

    def _create_row(self, key: str, bar: bool, presentation, section):
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        prominent = presentation in {"hero", "tile", "feature", "model"}
        stacked = prominent and presentation != "hero"
        line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL,
                       spacing=2 if stacked else 12)
        key_label = Gtk.Label()
        key_label.set_xalign(0)
        key_label.set_hexpand(True)
        key_label.set_wrap(True)
        key_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        key_label.set_max_width_chars(20)
        key_label.add_css_class("hw-key")
        value_label = Gtk.Label()
        value_label.set_xalign(0 if prominent or presentation == "note" else 1)
        value_label.set_wrap(True)
        value_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_label.set_max_width_chars(44)
        value_label.add_css_class("hw-value")
        if prominent:
            container.add_css_class("metric-" + presentation)
            value_label.add_css_class("metric-" + presentation + "-value")
            value_label.set_hexpand(presentation != "hero")
        if presentation in {"note", "model"}:
            key_label.set_visible(False)
        if presentation == "note":
            value_label.add_css_class("metric-note")
            value_label.set_hexpand(True)
        if presentation == "hero":
            key_label.set_valign(Gtk.Align.END)
            line.append(value_label)
            line.append(key_label)
        else:
            line.append(key_label)
            line.append(value_label)
        container.append(line)

        progress = None
        if bar:
            progress = Gtk.ProgressBar()
            progress.add_css_class("hw-bar")
            container.append(progress)

        if presentation == "tile":
            container.set_size_request(120, -1)
            self._section(section or "highlights").append(container)
            container.get_parent().set_focusable(False)
        elif presentation == "detail":
            self._section(section or "details", detail=True).append(container)
        else:
            self.body.append(container)
        self._rows[key] = (key_label, value_label, progress)
        return self._rows[key]

    def set_row(self, key, label, value, percent=None, level=None, bar=False,
                *, presentation="row", section=None) -> None:
        entry = self._rows.get(key) or self._create_row(key, bar, presentation, section)
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

