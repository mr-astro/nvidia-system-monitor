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
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from src.data.collector import Collector, Snapshot  # noqa: E402
from src.data.cpu import CpuMonitor  # noqa: E402
from src.data.nvidia import NvidiaMonitor  # noqa: E402
from src.data.ram import RamMonitor  # noqa: E402
from src.data.storage import StorageMonitor  # noqa: E402
from src.utils.config import Config  # noqa: E402
from src.utils.config import Config  # noqa: E402
from src.utils.formatting import (  # noqa: E402
    fmt, fmt_bytes, fmt_mb, fmt_temp, usage_level,
)
from src.utils.logger import setup_logging  # noqa: E402

APP_ID = "cl.mastro.NvidiaSystemMonitor"
PANEL_ORDER = ("cpu", "gpu", "ram", "storage")
PANEL_TITLES = {
    "cpu": "CPU",
    "gpu": "GPU",
    "ram": "RAM",
    "storage": "Almacenamiento",
}
LEVEL_CLASSES = ("usage-low", "usage-medium", "usage-high")
MAX_SENSOR_ROWS = 24


def build_css(_colors: dict) -> str:
    # CSS for mandatory dark mode based on user request.
    return """
window { 
    background-color: #1e1e1e; 
    color: #ffffff; 
}
headerbar {
    background-color: #2d2d2d;
    color: #ffffff;
    border-bottom: 1px solid #3d3d3d;
}
.card, .metric-card, frame, box.card {
    background-color: #2d2d2d;
    border: 1px solid #3d3d3d;
    border-radius: 8px;
    padding: 12px;
    margin: 6px;
}
headerbar label {
    color: #ffffff;
    font-weight: bold;
}
headerbar button {
    background-color: #3d3d3d;
    color: #ffffff;
    border: none;
    border-radius: 4px;
}
headerbar button:hover {
    background-color: #4d4d4d;
}
.hw-heading {{ font-weight: 800; font-size: 180%; }}
.hw-title {{ font-weight: 700; font-size: 130%; }}
.hw-key {{ opacity: 0.75; }}
.hw-value {{ font-feature-settings: "tnum"; }}
progressbar > trough, progressbar > trough > progress {{ min-height: 6px; }}
progressbar > trough > progress {{ background-color: #4CAF50; }}
"""


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

        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = True

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
        key_label.add_css_class("hw-key")
        value_label = Gtk.Label()
        value_label.set_xalign(1)
        value_label.set_wrap(True)
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = True

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


class SystemMonitorApp(Gtk.Application):
    def __init__(self, config=None):
        super().__init__(application_id=APP_ID)
        self.config = config or Config()
        self.logger = setup_logging(bool(self.config.get("logging_enabled", True)))

        panels = self.config.get("panels", {})
        self.enabled = [p for p in PANEL_ORDER if panels.get(p, True)]
        self.unit = self.config.get("temperature_unit", "C")
        thresholds = self.config.get("thresholds", {})
        self.medium = float(thresholds.get("medium_percent", 60))
        self.high = float(thresholds.get("high_percent", 85))
        self.interval = float(self.config.get("refresh_interval_seconds", 1.0))

        self.collector = Collector(
            cpu=CpuMonitor() if "cpu" in self.enabled else None,
            gpu=NvidiaMonitor() if "gpu" in self.enabled else None,
            ram=RamMonitor() if "ram" in self.enabled else None,
            storage=StorageMonitor() if "storage" in self.enabled else None,
            logger=self.logger,
        )

        self.window = None
        self.status = None
        self.cards: dict = {}
        self._last_sequence = 0
        self._last_render_error: dict = {}

    # ---- lifecycle ------------------------------------------------------

    def do_activate(self):
        if self.window is not None:
            self.window.present()
            return

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
        self.collector.stop()
        Gtk.Application.do_shutdown(self)

    # ---- construction ---------------------------------------------------

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        css = """
/* Ventana principal */
window {
    background-color: #1e1e1e;
    color: #e0e0e0;
}

/* Barra superior */
headerbar {
    background-color: #2d2d2d;
    color: #ffffff;
    border-bottom: 1px solid #3d3d3d;
}
headerbar label {
    color: #ffffff;
    font-weight: bold;
}
headerbar button {
    background-color: #3d3d3d;
    color: #ffffff;
    border: none;
    border-radius: 4px;
}
headerbar button:hover {
    background-color: #4d4d4d;
}

/* Tarjetas de hardware (CPU, GPU, RAM, Almacenamiento) */
.hw-card {
    background-color: #2d2d2d;
    border: 1px solid #3d3d3d;
    border-radius: 8px;
    padding: 12px;
    margin: 6px;
}

/* Títulos de las tarjetas (CPU, GPU, RAM, etc.) */
.hw-title, .hw-heading {
    color: #ffffff;
    font-weight: bold;
    font-size: 14px;
    margin-bottom: 8px;
}

/* Etiquetas de las métricas (Modelo, Uso, Temperatura, etc.) */
.hw-key {
    color: #b0b0b0;
    font-size: 12px;
}

/* Valores de las métricas (los datos numéricos) */
.hw-value {
    color: #e0e0e0;
    font-weight: 500;
    font-size: 12px;
}

/* Barras de progreso */
.hw-bar {
    min-height: 6px;
    border-radius: 3px;
}
.hw-bar > trough {
    background-color: #3d3d3d;
    border-radius: 3px;
}
.hw-bar > trough > progress {
    background-color: #4CAF50;
    border-radius: 3px;
}

/* Niveles de uso (probablemente usage-low, usage-medium, usage-high) */
.usage-low > trough > progress {
    background-color: #4CAF50;
}
.usage-medium > trough > progress {
    background-color: #FFA726;
}
.usage-high > trough > progress {
    background-color: #EF5350;
}

/* Etiquetas generales */
label {
    color: #e0e0e0;
}
"""
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
        self.window.set_title("NVIDIA System Monitor")
        self.window.set_default_size(1100, 760)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in (root.set_margin_top, root.set_margin_bottom,
                       root.set_margin_start, root.set_margin_end):
            setter(16)
        self.window.set_child(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title = Gtk.Label(label="Hardware Monitor")
        title.set_xalign(0)
        title.set_hexpand(True)
        title.add_css_class("hw-heading")
        header.append(title)
        self.status = Gtk.Label(label="Recopilando datos…")
        header.append(self.status)
        root.append(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroller)

        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.set_hexpand(True)
        grid.set_column_homogeneous(True)
        scroller.set_child(grid)

        if not self.enabled:
            grid.attach(Gtk.Label(label="Ningún panel habilitado en la configuración."), 0, 0, 2, 1)
        for position, name in enumerate(self.enabled):
            card = MetricCard(PANEL_TITLES[name])
            self.cards[name] = card
            grid.attach(card, position % 2, position // 2, 1, 1)

    # ---- update loop ----------------------------------------------------

    def _on_tick(self):
        if self.window is None:
            return GLib.SOURCE_REMOVE
        snapshot = self.collector.latest()
        if snapshot.sequence and snapshot.sequence != self._last_sequence:
            self._last_sequence = snapshot.sequence
            self._render(snapshot)
        self._update_status(snapshot)
        return GLib.SOURCE_CONTINUE

    def _update_status(self, snap: Snapshot) -> None:
        if not snap.sequence:
            return
        age = time.monotonic() - snap.timestamp
        if age > self.interval * 5 + 5:
            _set_text(self.status, "Sin datos recientes")
        elif snap.errors:
            _set_text(self.status, "Actualizado con avisos: " + ", ".join(sorted(snap.errors)))
        else:
            _set_text(self.status, "Actualizado " + time.strftime("%H:%M:%S"))
        self.status.set_tooltip_text(
            "\n".join(f"{k}: {v}" for k, v in snap.errors.items()) or None
        )

    def _render(self, snap: Snapshot) -> None:
        renderers = {
            "cpu": self._render_cpu,
            "gpu": self._render_gpu,
            "ram": self._render_ram,
            "storage": self._render_storage,
            "sensors": self._render_sensors,
        }
        for name, card in self.cards.items():
            try:
                renderers[name](card, snap)
            except Exception as exc:  # a rendering bug must not kill the loop
                message = f"{type(exc).__name__}: {exc}"
                if self._last_render_error.get(name) != message:
                    self._last_render_error[name] = message
                    self.logger.exception("Error dibujando el panel '%s'", name)

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
        card.set_row("model", "Modelo", c.model)
        card.set_row("usage", "Uso", fmt(c.usage_percent, "%"),
                     c.usage_percent, self._level(c.usage_percent), bar=True)
        card.set_row("freq", "Frecuencia", fmt(c.frequency_mhz, " MHz", 0))
        card.set_row("maxfreq", "Máxima", fmt(c.max_frequency_mhz, " MHz", 0))
        card.set_row("temp", "Temperatura", fmt_temp(c.temperature_c, self.unit))
        card.set_row("cores", "Núcleos / hilos", f"{c.cores} / {c.threads}")
        card.set_row("load", "Carga (1/5/15 min)",
                     f"{fmt(c.load_1m)} / {fmt(c.load_5m)} / {fmt(c.load_15m)}")

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
            card.set_row(p + "name", tag + "Modelo", g.name)
            card.set_row(p + "usage", tag + "Uso", fmt(g.utilization_percent, "%"),
                         g.utilization_percent, self._level(g.utilization_percent), bar=True)
            card.set_row(p + "vram", tag + "VRAM",
                         f"{fmt_mb(g.memory_used_mb)} / {fmt_mb(g.memory_total_mb)} "
                         f"({fmt(g.memory_percent, '%')})",
                         g.memory_percent, self._level(g.memory_percent), bar=True)
            card.set_row(p + "temp", tag + "Temperatura", fmt_temp(g.temperature_c, self.unit))
            card.set_row(p + "power", tag + "Consumo", fmt(g.power_draw_w, " W"))
            card.set_row(p + "limit", tag + "Límite potencia", fmt(g.power_limit_w, " W"))
            card.set_row(p + "core", tag + "Core clock", fmt(g.core_clock_mhz, " MHz", 0))
            card.set_row(p + "mem", tag + "Memory clock", fmt(g.memory_clock_mhz, " MHz", 0))
            card.set_row(p + "fan", tag + "Ventilador", fmt(g.fan_percent, "%"))
        card.set_row("driver", "Driver", snap.gpus[0].driver_version)
        card.set_row("cuda", "CUDA", snap.cuda_version)

    def _render_ram(self, card: MetricCard, snap: Snapshot) -> None:
        r = snap.ram
        if r is None or r.total_mb is None:
            return self._no_data(card)
        card.reset(("ram", bool(r.swap_total_mb)))
        card.set_row("total", "Total", fmt_mb(r.total_mb))
        card.set_row("used", "Usada", f"{fmt_mb(r.used_mb)} ({fmt(r.usage_percent, '%')})",
                     r.usage_percent, self._level(r.usage_percent), bar=True)
        card.set_row("avail", "Disponible", fmt_mb(r.available_mb))
        card.set_row("free", "Libre", fmt_mb(r.free_mb))
        card.set_row("cache", "Caché y buffers", fmt_mb(r.cached_mb))
        if r.swap_total_mb:
            card.set_row("swap", "Swap",
                         f"{fmt_mb(r.swap_used_mb)} / {fmt_mb(r.swap_total_mb)} "
                         f"({fmt(r.swap_usage_percent, '%')})",
                         r.swap_usage_percent, self._level(r.swap_usage_percent), bar=True)
        else:
            card.set_row("swap", "Swap", "No configurada")

    def _render_storage(self, card: MetricCard, snap: Snapshot) -> None:
        disks = snap.disks
        if not disks:
            return self._no_data(card, "No se detectaron discos")

        signature = ("storage", tuple(
            (d.name, tuple((p.name, p.depth, p.used_bytes is not None) for p in d.partitions))
            for d in disks
        ))
        card.reset(signature)
        for d in disks:
            card.set_row(
                f"disk:{d.name}", d.name,
                f"{d.kind} · {fmt_bytes(d.size_bytes)} · {fmt_temp(d.temperature_c, self.unit)}",
            )
            if not d.partitions:
                card.set_row(f"nopart:{d.name}", "  Particiones", "N/D")
            for p in d.partitions:
                indent = "  " * p.depth
                mount = ", ".join(p.mountpoints) if p.mountpoints else "Sin montaje"
                card.set_row(f"part:{d.name}:{p.name}", f"{indent}{p.name}",
                             f"{p.fstype} · {fmt_bytes(p.size_bytes)} · {mount}")
                if p.used_bytes is not None:
                    card.set_row(
                        f"use:{d.name}:{p.name}", f"{indent}  Uso",
                        f"{fmt_bytes(p.used_bytes)} usados · {fmt_bytes(p.free_bytes)} libres "
                        f"({fmt(p.usage_percent, '%')})",
                        p.usage_percent, self._level(p.usage_percent), bar=True,
                    )

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


def main() -> int:
    app = SystemMonitorApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
