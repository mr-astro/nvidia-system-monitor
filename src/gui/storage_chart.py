"""Persistent GTK/Cairo physical-layout view; consumes prepared byte geometry only."""
from dataclasses import replace
import math
import weakref

import gi
gi.require_version('Gtk', '4.0')
gi.require_foreign('cairo')
from gi.repository import GLib, Gtk, Pango

from src.gui.storage_chart_model import ChartModel, PartitionColors, prepare_chart
from src.gui.storage_usage_model import UsageModel
from src.gui.storage_usage_style import usage_style
from src.gui.widgets import _set_text


CHART_CSS = """
.storage-chart > flowboxchild { padding: 0; }
.storage-chart-center { font-size: 115%; font-weight: 700; }
.storage-chart-caption { font-size: 75%; opacity: 0.75; }
.storage-legend { font-size: 85%; }
.storage-chart-note { font-size: 80%; opacity: 0.8; }
.storage-chart-unavailable { font-weight: 600; }
.storage-swatch { font-size: 130%; }
.storage-root-usage { font-size: 125%; font-weight: 700; }
.storage-color-1, .monitor-window .storage-swatch.storage-color-1 { color: #36A8E0; }
.storage-color-2, .monitor-window .storage-swatch.storage-color-2 { color: #E6AB48; }
.storage-color-3, .monitor-window .storage-swatch.storage-color-3 { color: #C18BE5; }
.storage-color-4, .monitor-window .storage-swatch.storage-color-4 { color: #E87687; }
.storage-color-5, .monitor-window .storage-swatch.storage-color-5 { color: #40BBA8; }
.storage-color-6, .monitor-window .storage-swatch.storage-color-6 { color: #A6A0EF; }
.storage-color-7, .monitor-window .storage-swatch.storage-color-7 { color: #D3BA68; }
.storage-color-8, .storage-inner-unknown,
.monitor-window .storage-swatch.storage-color-8,
.monitor-window .storage-swatch.storage-inner-unknown { color: #8A969E; }
.storage-color-9, .monitor-window .storage-swatch.storage-color-9 { color: #B7947D; }
"""


def wrapping_label(css):
    widget = Gtk.Label(xalign=0)
    widget.set_hexpand(True)
    widget.set_wrap(True)
    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_max_width_chars(40)
    widget.add_css_class(css)
    return widget


def weak_callback(method):
    """Cairo/signal closures must not retain a removed card through its children."""
    reference = weakref.WeakMethod(method)
    def invoke(*args):
        callback = reference()
        if callback is not None:
            return callback(*args)
    return invoke


class _ChartArea(Gtk.DrawingArea):
    def __init__(self, owner):
        super().__init__()
        self.owner = weakref.ref(owner)

    def do_css_changed(self, change):
        Gtk.DrawingArea.do_css_changed(self, change)
        owner = self.owner() if hasattr(self, 'owner') else None
        if owner is not None:
            owner.style_changed()


class _Swatch(Gtk.Label):
    def __init__(self, owner):
        super().__init__(label='●')
        self.owner = weakref.ref(owner)

    def do_css_changed(self, change):
        Gtk.Label.do_css_changed(self, change)
        owner = self.owner() if hasattr(self, 'owner') else None
        if owner is not None:
            owner.style_changed()


class PhysicalDonut(Gtk.FlowBox):
    """GTK performs responsive reflow and HiDPI sizing, without polling/resizing I/O."""
    def __init__(self, usage_thresholds=None):
        super().__init__()
        self.add_css_class('storage-chart')
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.set_min_children_per_line(1)
        self.set_max_children_per_line(2)
        self.set_column_spacing(20)
        self.set_row_spacing(10)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)
        self.colors = PartitionColors()
        self.model = ChartModel()
        self.usage_model = UsageModel()
        self.usage_thresholds = usage_thresholds
        self.usage_style = usage_style(self.usage_model, usage_thresholds)
        self._last_layout = None
        self._last_current = None
        self._dirty = True
        self._render_error = None
        self._error_idle = 0
        self._style_idle = 0
        self._foreground = (0.5, 0.5, 0.5, 0.16)
        self._inner_neutral = (0.54, 0.59, 0.62, 1)
        self._arcs = ()
        self._usage_arcs = ()
        self.rows = {}
        self.summary_rows = {}
        self.visual = Gtk.Overlay()
        self.visual.set_valign(Gtk.Align.START)
        self.area = _ChartArea(self)
        self.area.set_content_width(164)
        self.area.set_content_height(164)
        self.area.set_draw_func(weak_callback(self._draw))
        self.area.connect('map', weak_callback(self._mapped))
        self.visual.set_child(self.area)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        self.capacity = Gtk.Label(label='N/D')
        self.capacity.add_css_class('storage-chart-center')
        center.append(self.capacity)
        caption = Gtk.Label(label='FÍSICA')
        caption.add_css_class('storage-chart-caption')
        center.append(caption)
        self.visual.add_overlay(center)
        self.legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.legend.set_size_request(220, -1)
        self.legend.set_valign(Gtk.Align.START)
        self.legend.set_hexpand(True)
        self.state = wrapping_label('storage-chart-unavailable')
        self.reason = wrapping_label('storage-chart-note')
        self.entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.summary_entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.more = wrapping_label('storage-chart-note')
        heading = wrapping_label('storage-chart-note')
        heading.set_label('Exterior · distribución física')
        self.note = wrapping_label('storage-chart-note')
        self.note.set_label('Distribución física; no indica espacio disponible del filesystem. '
                            '«Sin particionar» puede incluir metadatos de la tabla.')
        self.full_legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.full_legend.append(self.entries)
        self.full_legend.append(self.note)
        self.full_legend.set_visible(False)
        self.usage_scope = wrapping_label('storage-legend')
        self.usage_summary = wrapping_label('storage-legend')
        self.usage_status = wrapping_label('storage-chart-note')
        self.usage_values = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.used_swatch, self.free_swatch = _Swatch(self), _Swatch(self)
        self.used_swatch.set_css_classes(['storage-swatch', 'storage-inner-unknown'])
        self.free_swatch.set_css_classes(['storage-swatch', 'storage-color-8'])
        self.used_value, self.free_value = wrapping_label('storage-legend'), wrapping_label('storage-legend')
        # GtkExpander detaches its child when collapsed. Keep color probes in
        # the always-attached view so hidden technical rows cannot change arcs.
        palette = Gtk.Box()
        palette.set_visible(False)
        self.palette = []
        for slot in range(10):
            swatch = _Swatch(self)
            swatch.set_css_classes(['storage-swatch', f'storage-color-{slot}'])
            palette.append(swatch)
            self.palette.append(swatch)
        for child in (self.used_swatch, self.used_value, self.free_swatch, self.free_value):
            self.usage_values.append(child)
        for child in (heading, self.state, self.reason, self.summary_entries, self.more,
                      self.usage_scope, self.usage_values, self.usage_summary, self.usage_status, self.full_legend, palette):
            self.legend.append(child)
        self.append(self.visual)
        self.append(self.legend)
        self._show_model()
        self._show_usage()

    def _invalidate(self):
        self._dirty = True
        if self.area.get_mapped():
            self.area.queue_draw()
            self._dirty = False

    def _mapped(self, area):
        # A hidden stack page accumulates only the latest model. GTK also redraws
        # on allocation/style/scale changes; no tick callback or timer is needed.
        self._refresh_style()
        if self._dirty:
            area.queue_draw()
            self._dirty = False

    def style_changed(self):
        if not self._style_idle:
            self._style_idle = GLib.idle_add(self._apply_style)

    def _apply_style(self):
        self._style_idle = 0
        if self._refresh_style():
            self._invalidate()
        return GLib.SOURCE_REMOVE

    def _refresh_style(self):
        # Resolve theme/configuration outside Cairo, including live CSS changes.
        foreground = self.area.get_style_context().get_color()
        neutral = (foreground.red, foreground.green, foreground.blue, 0.16)
        arcs = []
        for segment in self.model.segments:
            color = self.palette[segment.color].get_style_context().get_color()
            arcs.append((segment.start, segment.end, (color.red, color.green, color.blue, color.alpha)))
        arcs = tuple(arcs)
        usage_arcs = []
        free = self.free_swatch.get_style_context().get_color()
        inner_neutral = (free.red, free.green, free.blue, free.alpha)
        fraction = self.usage_model.fraction
        if self.usage_style.level is not None:
            middle = -math.pi / 2 + math.tau * fraction
            for start, end, swatch in ((-math.pi / 2, middle, self.used_swatch),
                                       (middle, 3 * math.pi / 2, self.free_swatch)):
                color = swatch.get_style_context().get_color()
                usage_arcs.append((start, end, (color.red, color.green, color.blue, color.alpha)))
        usage_arcs = tuple(usage_arcs)
        changed = (neutral, inner_neutral, arcs, usage_arcs) != (self._foreground, self._inner_neutral, self._arcs, self._usage_arcs)
        self._inner_neutral = inner_neutral
        self._foreground, self._arcs, self._usage_arcs = neutral, arcs, usage_arcs
        return changed

    def update(self, layout, now, usage=None):
        current = bool(layout and layout.current(now))
        usage = usage if usage is not None else UsageModel()
        physical_change = layout != self._last_layout or current != self._last_current or self._render_error
        if not physical_change and usage == self.usage_model:
            return
        model = prepare_chart(layout, now, self.colors) if physical_change else self.model
        self._last_layout, self._last_current = layout, current
        recovering = bool(self._render_error)
        if self._error_idle:
            GLib.source_remove(self._error_idle)
            self._error_idle = 0
        self._render_error = None
        if model != self.model or recovering:
            self.model = model
            self._show_model()
        if usage != self.usage_model or recovering:
            self.usage_model = usage
            self._show_usage()
        if self._refresh_style() or recovering:
            self._invalidate()

    def _show_model(self):
        self.capacity.set_label(self.model.capacity)
        self.state.set_label('Distribución no disponible')
        self.state.set_visible(not self.model.available)
        self.reason.set_label(self.model.reason)
        self.reason.set_visible(not self.model.available)
        self.note.set_visible(self.model.available)
        self._accessibility()
        active, previous = set(), None
        for segment in self.model.segments:
            active.add(segment.key)
            row = self.rows.get(segment.key)
            if row is None:
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                swatch = _Swatch(self)
                swatch.set_valign(Gtk.Align.START)
                swatch.add_css_class('storage-swatch')
                text = wrapping_label('storage-legend')
                box.append(swatch)
                box.append(text)
                self.entries.append(box)
                row = self.rows[segment.key] = (box, swatch, text)
            box, swatch, text = row
            swatch.set_css_classes(['storage-swatch', f'storage-color-{segment.color}'])
            text.set_label(segment.text)
            self.entries.reorder_child_after(box, previous)
            previous = box
        for key in set(self.rows) - active:
            self.entries.remove(self.rows.pop(key)[0])
        shown, previous = set(), None
        for segment in self.model.segments[:3]:
            shown.add(segment.key)
            if segment.key not in self.summary_rows:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                swatch, text = _Swatch(self), wrapping_label('storage-legend')
                swatch.set_valign(Gtk.Align.START)
                row.append(swatch)
                row.append(text)
                self.summary_rows[segment.key] = (row, swatch, text)
                self.summary_entries.append(row)
            row, swatch, text = self.summary_rows[segment.key]
            swatch.set_css_classes(['storage-swatch', f'storage-color-{segment.color}'])
            text.set_label(segment.text)
            self.summary_entries.reorder_child_after(row, previous)
            previous = row
        for key in set(self.summary_rows) - shown:
            self.summary_entries.remove(self.summary_rows.pop(key)[0])
        remaining = len(self.model.segments) - len(shown)
        self.more.set_label(f'{remaining} {"región" if remaining == 1 else "regiones"} más · ver detalles')
        self.more.set_visible(remaining > 0)

    def _show_usage(self):
        usage = self.usage_model
        self.usage_style = usage_style(usage, self.usage_thresholds)
        css = 'storage-inner-' + (self.usage_style.level or 'unknown')
        if not self.used_swatch.has_css_class(css):
            self.used_swatch.set_css_classes(['storage-swatch', css])
        _set_text(self.usage_scope, usage.scope)
        _set_text(self.usage_summary, usage.summary)
        _set_text(self.usage_status, self.usage_style.label + ' · ' + usage.reason)
        if usage.root_selected:
            self.used_value.add_css_class('storage-root-usage')
        else:
            self.used_value.remove_css_class('storage-root-usage')
        available = self.usage_style.level is not None
        self.usage_values.set_visible(available)
        self.usage_summary.set_visible(not available)
        if available:
            _set_text(self.used_value, f'{usage.fraction * 100:.1f}% usado'.replace('.', ','))
            _set_text(self.free_value, f'{(1 - usage.fraction) * 100:.1f}% disponible'.replace('.', ','))
        for widget in (self.usage_scope, self.usage_summary, self.usage_values, self.usage_status):
            widget.set_tooltip_text(usage.details)
        self._accessibility()

    def _accessibility(self):
        physical = ('\n'.join(segment.text for segment in self.model.segments) if self.model.available
                    else 'Distribución no disponible · ' + self.model.reason)
        usage = self.usage_model
        text = 'Exterior · distribución física\n' + physical + '\nInterior\n' + '\n'.join(
            (usage.scope, usage.summary, self.usage_style.label, usage.reason, usage.details))
        self.area.set_tooltip_text(text)
        self.area.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def _paint_neutral(self, context, width, height):
        radius = max(0, min(width, height) / 2 - 9)
        if radius <= 0:
            return
        context.set_line_cap(0)  # BUTT: never enlarge tiny extents with round caps.
        for ring_radius, thickness, color in ((radius, 14, self._foreground),
                                               (max(0, radius - 20), 10, self._inner_neutral)):
            context.set_source_rgba(*color)
            context.set_line_width(thickness)
            context.new_path()
            context.arc(width / 2, height / 2, ring_radius, 0, math.tau)
            context.stroke()

    def _paint(self, context, width, height):
        self._paint_neutral(context, width, height)
        radius = max(0, min(width, height) / 2 - 9)
        for arcs, ring_radius, thickness in ((self._arcs, radius, 14),
                                               (self._usage_arcs, max(0, radius - 20), 10)):
            context.set_line_width(thickness)
            for start, end, color in arcs:
                context.set_source_rgba(*color)
                context.new_path()
                context.arc(width / 2, height / 2, ring_radius, start, end)
                context.stroke()

    def _draw(self, area, context, width, height):
        if self._render_error:
            self._paint_neutral(context, width, height)
            return
        # Atomic group: a failed painter cannot leave plausible partial sectors.
        context.save()
        context.push_group()
        try:
            self._paint(context, width, height)
        except Exception as exc:
            context.pop_group()
            context.restore()
            self._paint_neutral(context, width, height)
            if not self._error_idle:
                self._render_error = f'{type(exc).__name__}: {exc}'
                self._error_idle = GLib.idle_add(self._show_render_error)
            return
        context.pop_group_to_source()
        context.paint()
        context.restore()

    def _show_render_error(self):
        self._error_idle = 0
        self.model = replace(self.model, segments=(), available=False,
                             reason='Error al dibujar la distribución: ' + self._render_error)
        self.usage_model = replace(self.usage_model, fraction=None, state='error', summary='Error de gráfico',
                                   reason='No se muestran sectores tras un error de Cairo')
        self._show_model()
        self._show_usage()
        self._refresh_style()
        self._invalidate()
        return GLib.SOURCE_REMOVE
