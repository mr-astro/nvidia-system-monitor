"""GTK4 graphical system monitor.

Data is collected by a background thread (src.data.collector). The main loop
only reads the latest snapshot and updates existing widgets in place.
"""
from __future__ import annotations

import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from src.data.collector import Collector, Snapshot  # noqa: E402
from src.data.cpu import CpuMonitor  # noqa: E402
from src.data.nvidia import NvidiaMonitor  # noqa: E402
from src.data.ram import RamMonitor  # noqa: E402
from src.data.sensors import SensorMonitor  # noqa: E402
from src.data.storage import StorageMonitor  # noqa: E402
from src.gui.widgets import MetricCard, _set_text
from src.gui.storage import StoragePanel, STORAGE_CSS
from src.data.storage_presentation import StorageAliasWorker
from src.utils.channel import DEFAULT_CHANNEL, STABLE, get_channel
from src.utils.config import Config  # noqa: E402
from src.utils.formatting import (  # noqa: E402
    fmt, fmt_mb, fmt_temp, usage_level,
)
from src.utils.logger import setup_logging  # noqa: E402

APP_ID = STABLE.application_id
PANEL_ORDER = ("cpu", "gpu", "ram", "storage", "sensors")
PANEL_TITLES = {
    "cpu": "CPU",
    "gpu": "GPU",
    "ram": "RAM",
    "storage": "Almacenamiento",
    "sensors": "Sensores",
}
MAX_SENSOR_ROWS = 24


LEFT_PANELS = ("cpu", "ram", "sensors")
RIGHT_PANELS = ("gpu",)
MIN_COLUMN_WIDTH = 340

_BASE_CSS = """
.hw-heading {{ font-size: 115%; font-weight: 700; }}
.hw-title {{ font-weight: 700; margin-bottom: 6px; }}
.hw-card {{ padding: 12px; border-radius: 12px; }}
.hw-key {{ font-size: 85%; }}
.hw-value {{ font-size: 85%; font-weight: 500; font-feature-settings: "tnum"; }}
.monitor-status {{ font-size: 85%; opacity: 0.85; }}
.monitor-switcher {{ margin-bottom: 2px; }}
.monitor-switcher button {{ padding: 9px 16px; border-radius: 8px; }}
.monitor-switcher label {{ font-size: 85%; font-weight: 700; }}
.monitor-empty {{ padding: 24px 12px; opacity: 0.8; }}
.monitor-layout > flowboxchild {{ padding: 0; }}
.metric-highlights > flowboxchild {{ padding: 0; }}
.metric-hero {{ margin: 2px 0 4px; }}
.metric-hero-value {{ font-size: 275%; font-weight: 700; }}
.metric-tile {{ padding: 4px 0; }}
.metric-tile-value {{ font-size: 150%; font-weight: 600; }}
.metric-feature {{ margin: 4px 0; }}
.metric-feature-value {{ font-size: 115%; font-weight: 600; }}
.metric-model {{ margin-bottom: 4px; }}
.metric-model-value {{ font-size: 100%; font-weight: 600; }}
.metric-note {{ font-size: 80%; opacity: 0.75; font-weight: 400; }}
.metric-details {{ margin-top: 8px; font-size: 90%; }}
.metric-details > title {{ padding: 4px 0; }}
.monitor-channel {{ font-size: 85%; font-weight: 700; border-radius: 6px; padding: 4px 8px; }}
progressbar.hw-bar trough, progressbar.hw-bar progress {{ min-height: 6px; border-radius: 3px; }}
progressbar.usage-low progress {{ background-image: none; background-color: {low}; }}
progressbar.usage-medium progress {{ background-image: none; background-color: {medium}; }}
progressbar.usage-high progress {{ background-image: none; background-color: {high}; }}
"""

# Neutral card used with the system theme (works on light and dark).
_SYSTEM_CSS = """
.hw-card {
    border: 1px solid rgba(128, 128, 128, 0.35);
    background-color: rgba(128, 128, 128, 0.08);
}
.hw-key { opacity: 0.75; }
.monitor-switcher button:checked { font-weight: 700; }
"""

# Explicit dark palette. Independent of the GTK version (the
# gtk-application-prefer-dark-theme setting is deprecated since GTK 4.20).
_DARK_CSS = """
.monitor-window.monitor-dark { background-color: #101410; color: #F2F5F2; }
.monitor-window.monitor-dark headerbar { background-color: #1B231C; color: #F2F5F2; border-bottom: 1px solid #303C31; }
.monitor-window.monitor-dark label { color: #F2F5F2; }
.monitor-window.monitor-dark .hw-card { background-color: #1B231C; border: 1px solid #303C31; }
.monitor-window.monitor-dark .hw-key,
.monitor-window.monitor-dark .monitor-status,
.monitor-window.monitor-dark .storage-secondary,
.monitor-window.monitor-dark .storage-status { color: #A5B0A5; }
.monitor-window.monitor-dark progressbar.hw-bar trough { background-color: #303C31; }
.monitor-window.monitor-dark .monitor-switcher button {
    background-image: none; background-color: #1B231C;
    border: 1px solid #303C31; box-shadow: none;
}
.monitor-window.monitor-dark .monitor-switcher button:hover { background-color: #283529; }
.monitor-window.monitor-dark .monitor-switcher button:checked { background-color: #76B900; border-color: #76B900; }
.monitor-window.monitor-dark .monitor-switcher button:checked label { color: #101410; }
.monitor-window.monitor-dark .storage-panel { background-color: transparent; border: none; }
.monitor-window.monitor-dark .monitor-channel { color: #76B900; background-color: #1B231C; }
"""


def build_css(colors: dict, dark: bool = True) -> str:
    base = _BASE_CSS.format(
        low=colors["low_usage_color"],
        medium=colors["medium_usage_color"],
        high=colors["high_usage_color"],
    )
    return base + (_DARK_CSS if dark else _SYSTEM_CSS) + STORAGE_CSS


class SystemMonitorApp(Gtk.Application):
    def __init__(self, config=None, *, channel=DEFAULT_CHANNEL):
        self.channel = get_channel(channel)
        super().__init__(application_id=self.channel.application_id)
        self.config = config or Config(channel=channel)
        self.disk_aliases = StorageAliasWorker(channel=channel)
        self.logger = setup_logging(bool(self.config.get("logging_enabled", True)), channel=channel)

        panels = self.config.get("panels", {})
        self.enabled = [p for p in PANEL_ORDER if panels.get(p, True)]
        self.unit = self.config.get("temperature_unit", "C")
        thresholds = self.config.get("thresholds", {})
        self.medium = float(thresholds.get("medium_percent", 60))
        self.high = float(thresholds.get("high_percent", 85))
        self.interval = float(self.config.get("refresh_interval_seconds", 1.0))
        self.dark = self.config.get("theme", "dark") == "dark"

        self.collector = Collector(
            cpu=CpuMonitor() if "cpu" in self.enabled else None,
            gpu=NvidiaMonitor() if "gpu" in self.enabled else None,
            ram=RamMonitor() if "ram" in self.enabled else None,
            storage=StorageMonitor() if "storage" in self.enabled else None,
            sensors=SensorMonitor() if "sensors" in self.enabled else None,
            interval=self.interval,
            logger=self.logger,
            storage_aliases=self.disk_aliases if "storage" in self.enabled else None,
        )

        self.window = None
        self.status = None
        self.cards: dict = {}
        self._last_sequence = 0
        self._last_snapshot = Snapshot()
        self._last_age_render = None
        self._last_render_error: dict = {}
        self._render_attempts: dict = {}

    # ---- lifecycle ------------------------------------------------------

    def do_activate(self):
        if self.window is not None:
            self.window.present()
            return

        self._apply_theme()
        self._install_css()
        self._build_window()
        self.collector.start()
        self.window.present()

        tick_ms = int(max(250, min(1000, self.interval * 500)))
        GLib.timeout_add(tick_ms, self._on_tick)
        self.logger.info(
            "Monitor iniciado (intervalo %.1fs, paneles: %s)",
            self.interval, ", ".join(self.enabled) or "ninguno",
        )

    def do_shutdown(self):
        self.collector.stop(timeout=0)
        Gtk.Application.do_shutdown(self)

    # ---- construction ---------------------------------------------------

    def _apply_theme(self) -> None:
        """Dark chrome (scrollbars, title bar) on GTK < 4.20; the CSS covers the rest."""
        if not self.dark:
            return
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        try:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        except (TypeError, ValueError):
            pass  # property removed in a future GTK: the explicit CSS still applies

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        css = build_css(self.config.get("color_scheme"), self.dark)
        try:
            provider.load_from_string(css)  # GTK >= 4.12
        except AttributeError:
            provider.load_from_data(css.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_window(self) -> None:
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.add_css_class("monitor-window")
        if self.dark:
            self.window.add_css_class("monitor-dark")
        self.window.set_title(self.channel.title)
        self.window.set_icon_name(self.channel.icon_name)
        self.window.set_default_size(880, 700)
        self.window.set_size_request(380, 320)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for setter in (root.set_margin_top, root.set_margin_bottom,
                       root.set_margin_start, root.set_margin_end):
            setter(14)
        self.window.set_child(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title = Gtk.Label(label="DEV" if self.channel.name == "dev" else "SISTEMA")
        title.set_tooltip_text(self.channel.title)
        title.set_xalign(0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("monitor-channel")
        header.append(title)
        self.status = Gtk.Label(label="Recopilando datos…")
        self.status.add_css_class("monitor-status")
        self.status.set_xalign(1)
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(40)
        header.append(self.status)
        root.append(header)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.switcher = Gtk.StackSwitcher()
        self.switcher.add_css_class("monitor-switcher")
        self.switcher.set_halign(Gtk.Align.CENTER)
        self.switcher.set_stack(self.stack)
        root.append(self.switcher)
        root.append(self.stack)

        # Pages and cards stay attached for the lifetime of the window. Switching
        # pages changes visibility only; the existing render loop updates both.
        self.pages = {}
        contents = {}
        for name, page_title in (("performance", "RENDIMIENTO"), ("storage", "ALMACENAMIENTO")):
            scroller = Gtk.ScrolledWindow()
            scroller.set_hexpand(True)
            scroller.set_vexpand(True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            scroller.set_child(content)
            self.pages[name] = scroller
            contents[name] = content
            self.stack.add_titled(scroller, name, page_title)

        # GTK reflows the two independent groups using their minimum widths.
        # Unequal row heights keep the single-column layout compact. Cards never
        # leave their group, so resizing preserves their widgets and expanders.
        columns = Gtk.FlowBox()
        columns.add_css_class("monitor-layout")
        columns.set_selection_mode(Gtk.SelectionMode.NONE)
        columns.set_min_children_per_line(1)
        columns.set_max_children_per_line(2)
        columns.set_homogeneous(False)
        columns.set_column_spacing(12)
        columns.set_row_spacing(12)
        columns.set_valign(Gtk.Align.START)
        for group in (LEFT_PANELS, RIGHT_PANELS):
            names = [n for n in group if n in self.enabled]
            if not names:
                continue
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            column.set_hexpand(True)
            column.set_valign(Gtk.Align.START)
            column.set_size_request(MIN_COLUMN_WIDTH, -1)
            for name in names:
                self.cards[name] = self._new_card(name)
                column.append(self.cards[name])
            columns.append(column)
            column.get_parent().set_focusable(False)
        if columns.get_first_child() is not None:
            # A single enabled group should fill the row, while two groups keep
            # independent heights when they wrap onto separate lines.
            columns.set_homogeneous(columns.get_first_child() is columns.get_last_child())
            contents["performance"].append(columns)
        else:
            self._append_empty(contents["performance"], "No hay paneles de rendimiento habilitados.")

        if "storage" in self.enabled:
            self.cards["storage"] = self._new_card("storage")
            contents["storage"].append(self.cards["storage"])
        else:
            self._append_empty(contents["storage"], "El panel de almacenamiento está deshabilitado.")

    @staticmethod
    def _append_empty(content, text):
        empty = Gtk.Label(label=text)
        empty.set_wrap(True)
        empty.add_css_class("monitor-empty")
        content.append(empty)

    def _new_card(self, name):
        if name == "storage":
            return StoragePanel(self.unit, self.medium, self.high)
        return MetricCard(PANEL_TITLES[name])

    # ---- update loop ----------------------------------------------------

    def _on_tick(self):
        if self.window is None:
            return GLib.SOURCE_REMOVE
        snapshot = self.collector.latest()
        if snapshot.sequence < self._last_sequence:
            snapshot = self._last_snapshot
        else:
            self._last_snapshot = snapshot
        second = int(time.monotonic())
        if snapshot.sequence and (snapshot.sequence != self._last_sequence or second != self._last_age_render):
            self._last_sequence = snapshot.sequence
            self._last_age_render = second
            self._render(snapshot)
        self._update_status(snapshot)
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _source_note(state, now):
        age = state.age(now)
        if age is None:
            return "Sin medición válida" + (f" · {state.error}" if state.error else "")
        prefix = "Obsoleto" if state.stale(now) else "Medido"
        return f"{prefix} · hace {age:.0f} s" + (f" · {state.error}" if state.error else "")

    @staticmethod
    def _source_title(key):
        if key in PANEL_TITLES:
            return PANEL_TITLES[key]
        if key == "topology":
            return "Topología de almacenamiento"
        if key == "cuda":
            return "NVIDIA/CUDA"
        prefix, separator, device = key.partition(":")
        names = {"smart": "SMART", "udisks": "Temperatura UDisks2",
                 "temperature": "Temperatura", "usage": "Uso", "interfaz": "Interfaz"}
        if separator and prefix in names:
            return f"{names[prefix]} · {PANEL_TITLES.get(device, device)}"
        return key

    def _update_status(self, snap: Snapshot) -> None:
        if not snap.sequence:
            return
        now = time.monotonic()
        errors = dict(snap.errors)
        errors.update({f"interfaz:{k}": v for k, v in self._last_render_error.items()})
        stale = {k: v for k, v in snap.sources.items() if v.stale(now)}
        received = f"Instantánea recibida hace {max(0, now - snap.timestamp):.0f} s"
        details = [received]
        details.extend(f"{self._source_title(k)} ({k}): {v}" for k, v in errors.items())
        details.extend(f"{self._source_title(k)} ({k}): {self._source_note(v, now)}"
                       for k, v in snap.sources.items())
        if errors or stale:
            _set_text(self.status, "Avisos: " + ", ".join(
                self._source_title(k) for k in sorted(set(errors) | set(stale))))
        elif snap.sources and all(v.acquired_at is None for v in snap.sources.values()):
            _set_text(self.status, "Sin mediciones válidas")
        elif now - snap.timestamp > self.interval * 5 + 5:
            _set_text(self.status, "Sin instantáneas recientes")
        else:
            _set_text(self.status, received)
        self.status.set_tooltip_text("\n".join(details))

    def _render(self, snap: Snapshot) -> None:
        renderers = {
            "cpu": self._render_cpu,
            "gpu": self._render_gpu,
            "ram": self._render_ram,
            "storage": self._render_storage,
            "sensors": self._render_sensors,
        }
        for name, card in self.cards.items():
            if name in self._last_render_error and self._render_attempts.get(name) == snap.sequence:
                continue
            self._render_attempts[name] = snap.sequence
            target = card
            # Retry into a detached card so a repeated failure cannot destroy the
            # visible error row or expose partially drawn values.
            if name in self._last_render_error:
                card = self._new_card(name)
                if name == "storage":
                    card.expansion_state = dict(target.expansion_state)
            try:
                state = snap.sources.get(name)
                now = time.monotonic()
                if name in {"cpu", "ram", "gpu", "sensors"} and state and state.stale(now):
                    card.reset(("stale", name))
                    card.set_row("state", "Estado", self._source_note(state, now))
                else:
                    renderers[name](card, snap)
                    if state and name != "storage":
                        card.set_row("freshness", "Medición", self._source_note(state, now), presentation="note")
                if card is not target:
                    target.remove(target.body)
                    body = card.body
                    card.remove(body)
                    target.body = body
                    target.append(body)
                    target._rows = card._rows
                    target._sections = card._sections
                    target._signature = card._signature
                    if name == "storage":
                        target.disk_cards = card.disk_cards
                        target.expansion_state = card.expansion_state
                        target._expansion_keys = card._expansion_keys
                        target._last_snapshot = card._last_snapshot
                self._last_render_error.pop(name, None)
            except Exception as exc:  # a rendering bug must not kill the loop
                message = f"{type(exc).__name__}: {exc}"
                if self._last_render_error.get(name) != message:
                    self._last_render_error[name] = message
                    self.logger.exception("Error dibujando el panel '%s'", name)
                target.reset(("render-error", name))
                target.set_row("state", "Estado", "Error al mostrar las métricas")

    def _level(self, percent):
        return usage_level(percent, self.medium, self.high)

    @staticmethod
    def _no_data(card: MetricCard, text: str = "Sin datos") -> None:
        card.reset(("no-data", text))
        card.set_row("state", "Estado", text)

    # ---- panels ---------------------------------------------------------

    def _render_cpu(self, card: MetricCard, snap: Snapshot) -> None:
        c = snap.cpu
        if c is None:
            return self._no_data(card)
        card.reset(("cpu",))
        card.set_row("model", "Procesador", c.model, presentation="model")
        card.set_row("usage", "Uso", fmt(c.usage_percent, "%"),
                     c.usage_percent, self._level(c.usage_percent), bar=True, presentation="hero")
        card.set_row("temp", "Temperatura", fmt_temp(c.temperature_c, self.unit), presentation="tile")
        card.set_row("freq", "Frecuencia", fmt(c.frequency_mhz, " MHz", 0), presentation="tile")
        card.set_row("maxfreq", "Máxima", fmt(c.max_frequency_mhz, " MHz", 0), presentation="detail")
        card.set_row("cores", "Núcleos / hilos", f"{c.cores} / {c.threads}", presentation="detail")
        card.set_row("load", "Carga (1/5/15 min)",
                     f"{fmt(c.load_1m)} / {fmt(c.load_5m)} / {fmt(c.load_15m)}", presentation="detail")

    def _render_gpu(self, card: MetricCard, snap: Snapshot) -> None:
        if not snap.gpus:
            text = "nvidia-smi no disponible" if not snap.gpu_available \
                else "nvidia-smi no responde"
            return self._no_data(card, text)

        multi = len(snap.gpus) > 1
        card.reset(("gpu", len(snap.gpus)))
        for position, g in enumerate(snap.gpus):
            p = f"gpu{position}:"
            tag = f"GPU {g.index if g.index is not None else position} · " if multi else ""
            card.set_row(p + "name", tag + "Modelo", g.name, presentation="model")
            card._rows[p + "name"][0].set_visible(multi)
            card.set_row(p + "usage", tag + "Uso", fmt(g.utilization_percent, "%"),
                         g.utilization_percent, self._level(g.utilization_percent), bar=True, presentation="hero")
            card.set_row(p + "vram", tag + "VRAM",
                         f"{fmt_mb(g.memory_used_mb)} / {fmt_mb(g.memory_total_mb)} "
                         f"({fmt(g.memory_percent, '%')})",
                         g.memory_percent, self._level(g.memory_percent), bar=True, presentation="feature")
            card.set_row(p + "temp", tag + "Temperatura", fmt_temp(g.temperature_c, self.unit),
                         presentation="tile", section=p + "highlights")
            card.set_row(p + "power", tag + "Consumo", fmt(g.power_draw_w, " W"),
                         presentation="tile", section=p + "highlights")
        # Keep all technical values accessible below the GPU summaries.
        for position, g in enumerate(snap.gpus):
            p = f"gpu{position}:"
            tag = f"GPU {g.index if g.index is not None else position} · " if multi else ""
            card.set_row(p + "limit", tag + "Límite potencia", fmt(g.power_limit_w, " W"), presentation="detail")
            card.set_row(p + "core", tag + "Core clock", fmt(g.core_clock_mhz, " MHz", 0), presentation="detail")
            card.set_row(p + "mem", tag + "Memory clock", fmt(g.memory_clock_mhz, " MHz", 0), presentation="detail")
            card.set_row(p + "fan", tag + "Ventilador", fmt(g.fan_percent, "%"), presentation="detail")
        card.set_row("driver", "Driver", snap.gpus[0].driver_version, presentation="detail")
        card.set_row("cuda", "CUDA", snap.cuda_version or "N/D", presentation="detail")

    def _render_ram(self, card: MetricCard, snap: Snapshot) -> None:
        r = snap.ram
        if r is None or r.total_mb is None:
            return self._no_data(card)
        card.reset(("ram", bool(r.swap_total_mb)))
        card.set_row("used", "Memoria en uso", fmt(r.usage_percent, '%'),
                     r.usage_percent, self._level(r.usage_percent), bar=True, presentation="hero")
        card.set_row("allocation", "Asignación", f"{fmt_mb(r.used_mb)} usados de {fmt_mb(r.total_mb)}", presentation="note")
        card.set_row("avail", "Disponible", fmt_mb(r.available_mb), presentation="tile")
        card.set_row("total", "Total", fmt_mb(r.total_mb), presentation="tile")
        if r.swap_total_mb:
            card.set_row("swap", "Swap",
                         f"{fmt_mb(r.swap_used_mb)} / {fmt_mb(r.swap_total_mb)} "
                         f"({fmt(r.swap_usage_percent, '%')})",
                         r.swap_usage_percent, self._level(r.swap_usage_percent), bar=True, presentation="feature")
        else:
            card.set_row("swap", "Swap", "No configurada")
        card.set_row("free", "Libre", fmt_mb(r.free_mb), presentation="detail")
        card.set_row("cache", "Caché y buffers", fmt_mb(r.cached_mb), presentation="detail")

    def _render_storage(self, card: StoragePanel, snap: Snapshot) -> None:
        card.update(snap, time.monotonic())

    def _render_sensors(self, card: MetricCard, snap: Snapshot) -> None:
        readings = snap.sensors
        if not readings:
            return self._no_data(card, "Sin lecturas (¿lm_sensors instalado?)")
        shown = readings[:MAX_SENSOR_ROWS]
        card.reset(("sensors", tuple((r.chip, r.label) for r in shown)))
        for position, r in enumerate(shown):
            card.set_row(f"s{position}", f"{r.chip} · {r.label}", fmt_temp(r.value_c, self.unit))
        if len(readings) > len(shown):
            card.set_row("more", "…", f"{len(readings) - len(shown)} sensores más")


def main(channel=DEFAULT_CHANNEL, argv=None) -> int:
    app = SystemMonitorApp(channel=channel)
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
