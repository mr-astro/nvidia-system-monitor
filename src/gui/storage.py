"""Compact storage presentation. Acquisition and freshness rules live in data/."""
from __future__ import annotations

import math
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from src.gui.widgets import MetricCard, _set_text, LEVEL_CLASSES
from src.data.storage import NON_FILESYSTEM_TYPES
from src.utils.disk_aliases import DiskIdentity
from src.utils.formatting import fmt_bytes, fmt_temp, usage_level

STORAGE_CSS = """
.storage-panel { padding: 0; background-color: transparent; border: none; }
.storage-disk { padding: 14px; }
.storage-disk expander > title { padding: 0; border-radius: 6px; }
.storage-main { font-weight: 700; }
.storage-title { font-size: 115%; font-weight: 700; }
.storage-capacity { font-size: 160%; font-weight: 700; }
.storage-temperature { font-size: 110%; font-weight: 600; }
.storage-caption { font-size: 75%; opacity: 0.75; margin-top: 4px; }
.storage-overview { margin: 8px 0; }
.storage-details > title { padding: 4px 0; font-size: 90%; }
.storage-secondary { font-size: 85%; opacity: 0.8; }
.storage-status { font-size: 85%; }
.storage-partition { padding: 10px 0 6px 12px; }
.storage-stale { opacity: 0.7; }
.storage-partition progressbar trough, .storage-partition progressbar progress { min-height: 6px; }
.storage-disk progressbar.hw-bar trough, .storage-disk progressbar.hw-bar progress { min-height: 6px; }
"""


def label(css=None):
    widget = Gtk.Label(xalign=0)
    widget.set_hexpand(True)
    widget.set_ellipsize(Pango.EllipsizeMode.END)
    widget.set_max_width_chars(48)
    if css:
        widget.add_css_class(css)
    return widget


def set_label(widget, text, tooltip=None):
    _set_text(widget, text)
    widget.set_tooltip_text(text if tooltip is None else tooltip)


def age_text(state, now):
    age = state.age(now) if state else None
    return f"hace {age:.0f} s" if age is not None else "sin medición válida"


def state_detail(state, now):
    if not state:
        return "N/D"
    prefix = "Obsoleto" if state.stale(now) else "Medido"
    return f"{prefix} · {age_text(state, now)}" + (f" · {state.error}" if state.error else "")


def brief_error(key, message):
    lowered = message.lower()
    if key.startswith("smart:"):
        if any(word in lowered for word in ("permission", "permis", "operation not permitted")):
            return "SMART: sin permisos"
        if "no disponible" in lowered or "not found" in lowered:
            return "SMART: no disponible"
        return "SMART: error de consulta"
    if key == "topology":
        return "Topología sin actualizar"
    if key.startswith("usage:"):
        return "Uso no disponible" if "montaje" in lowered else "Uso obsoleto"
    return "Error de adquisición"


def partition_title(partition):
    value = partition.label
    if isinstance(value, str) and value.strip():
        return value.strip()
    return partition.name


def usage_text(partition):
    percent = partition.usage_percent
    prefix = f"{percent:.1f}% · " if percent is not None and math.isfinite(percent) else ""
    return (prefix + f"{fmt_bytes(partition.used_bytes)} usados · "
            f"{fmt_bytes(partition.free_bytes)} disponibles").replace('.', ',')


def update_bar(bar, percent, medium, high):
    bar.set_fraction(max(0, min(1, percent / 100)))
    bar.set_tooltip_text(f"{percent:.1f}%".replace('.', ','))
    level = usage_level(percent, medium, high)
    for css in LEVEL_CLASSES:
        if css == f"usage-{level}":
            bar.add_css_class(css)
        else:
            bar.remove_css_class(css)


def display_partitions(partitions):
    """Merge repeated btrfs nodes only when technical identity/readings agree.

    A normal lsblk node already groups its subvolume mounts. This also handles
    repeated representations without summing or deduplicating by equal sizes.
    """
    result, btrfs = [], {}
    for part in partitions:
        key = (part.name, part.fstype, part.used_bytes, part.free_bytes, part.usage_percent)
        if part.fstype == "btrfs" and key in btrfs:
            index = btrfs[key]
            first = result[index]
            result[index] = replace(first, mountpoints=list(dict.fromkeys(first.mountpoints + part.mountpoints)))
        else:
            if part.fstype == "btrfs":
                btrfs[key] = len(result)
            result.append(part)
    return result


class PartitionRow(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.add_css_class("storage-partition")
        self.title = label("storage-main")
        self.details = label("storage-secondary")
        self.usage = label("storage-secondary")
        self.warning = label("storage-status")
        self.bar = Gtk.ProgressBar()
        self.bar.add_css_class("hw-bar")
        for child in (self.title, self.details, self.usage, self.warning, self.bar):
            self.append(child)

    def update(self, partition, state, now, medium, high):
        set_label(self.title, partition_title(partition))
        mount = ", ".join(partition.mountpoints) if partition.mountpoints else "Sin montar"
        shared = " · espacio compartido" if partition.fstype == "btrfs" and len(partition.mountpoints) > 1 else ""
        unavailable = " · Uso no disponible" if not partition.mountpoints else ""
        set_label(self.details, f"{partition.name} · {partition.fstype} · "
                  f"{fmt_bytes(partition.size_bytes)} · {mount}{shared}{unavailable}")
        self.set_margin_start(max(0, partition.depth - 1) * 12)
        has_usage = bool(partition.mountpoints) and partition.used_bytes is not None
        set_label(self.usage, usage_text(partition))
        self.usage.set_visible(has_usage)
        has_bar = has_usage and partition.usage_percent is not None and math.isfinite(partition.usage_percent)
        self.bar.set_visible(has_bar)
        if has_bar:
            update_bar(self.bar, partition.usage_percent, medium, high)
        stale = bool(state and state.stale(now))
        self.warning.set_visible(stale)
        if stale:
            message = "Uso obsoleto" if has_usage else "Uso no disponible"
            set_label(self.warning, f"{message} · {age_text(state, now)}", state_detail(state, now))
            self.add_css_class("storage-stale")
        else:
            self.remove_css_class("storage-stale")


class DiskCard(Gtk.Box):
    def __init__(self, key, expanded=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.key = key
        self.add_css_class("hw-card")
        self.add_css_class("storage-disk")
        self.set_hexpand(True)
        self.expander = Gtk.Expander()
        self.expander.set_label("Particiones y detalles")
        self.expander.add_css_class("storage-details")
        self.expander.set_expanded(expanded)
        self.expander.set_hexpand(True)
        self.title = label("storage-title")
        self.temperature = Gtk.Label(xalign=0)
        self.temperature.set_wrap(True)
        self.temperature.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.temperature.set_max_width_chars(10)
        self.temperature.set_halign(Gtk.Align.START)
        self.temperature.add_css_class("storage-temperature")
        self.technical = label("storage-secondary")
        self.model = label("storage-secondary")
        self.status = label("storage-status")
        self.capacity = Gtk.Label(xalign=0)
        self.capacity.add_css_class("storage-capacity")
        self.device_summary = label("storage-main")
        self.partition_summary = label("storage-secondary")
        self.usage_scope = label("storage-secondary")
        self.usage = label("storage-secondary")
        self.usage_bar = Gtk.ProgressBar()
        self.usage_bar.add_css_class("hw-bar")
        self.usage_warning = label("storage-status")
        for child in (self.title, self.model):
            self.append(child)
        # This compact region already carries real data. A later phase can add
        # a donut here without reserving a blank canvas or changing reconciliation.
        self.overview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.overview.add_css_class("storage-overview")
        self.capacity_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.capacity_box.set_size_request(110, -1)
        self.capacity_box.set_valign(Gtk.Align.START)
        for text, widget in (("CAPACIDAD", self.capacity), ("TEMPERATURA", self.temperature)):
            caption = Gtk.Label(label=text, xalign=0)
            caption.add_css_class("storage-caption")
            self.capacity_box.append(caption)
            self.capacity_box.append(widget)
        self.summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.summary.set_hexpand(True)
        for child in (self.device_summary, self.partition_summary, self.usage_scope,
                      self.usage, self.usage_warning, self.usage_bar, self.status):
            self.summary.append(child)
        self.overview.append(self.capacity_box)
        self.overview.append(self.summary)
        self.append(self.overview)
        self.partitions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.partitions_box.append(self.technical)
        self.expander.set_child(self.partitions_box)
        self.append(self.expander)
        self.partition_rows = {}
        self.empty = label("storage-secondary")
        self.empty.set_label("Sin particiones")
        self.partitions_box.append(self.empty)

    def _update_usage(self, partitions, snap, now, medium, high, direct=None):
        # Container devices have no filesystem usage of their own. Unknown
        # leaves count conservatively; unmounted filesystems still count.
        filesystems = ([direct] if direct is not None else
                       [part for part in partitions if part.fstype not in NON_FILESYSTEM_TYPES])
        self.usage_bar.set_visible(False)
        self.usage_scope.set_visible(False)
        self.usage_warning.set_visible(False)
        self.usage_bar.remove_css_class("storage-stale")
        if len(filesystems) > 1:
            set_label(self.usage, "Uso por particiones", "Sistemas de archivos independientes; consulte sus barras al desplegar.")
            return
        if not filesystems:
            set_label(self.usage, "Uso no disponible")
            return
        part = filesystems[0]
        name = partition_title(part)
        technical = f" ({part.name})" if name != part.name else ""
        if direct is not None:
            mount = ", ".join(part.mountpoints) or "Sin montar"
            shared = " · espacio compartido" if part.fstype == "btrfs" and len(part.mountpoints) > 1 else ""
            self.usage_scope.set_visible(True)
            set_label(self.usage_scope, f"Uso de {name}{technical} · sistema de archivos en disco completo"
                      f" · {part.fstype} · {mount}{shared}")
        state = snap.sources.get("usage:" + part.name)
        values = (part.used_bytes, part.free_bytes, part.usage_percent)
        valid = (bool(part.mountpoints) and state is not None and state.acquired_at is not None
                 and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value) and value >= 0 for value in values)
                 and part.usage_percent <= 100)
        if not valid:
            set_label(self.usage, "Uso no disponible", state_detail(state, now))
            return
        if direct is None:
            self.usage_scope.set_visible(True)
            set_label(self.usage_scope, f"Uso de {name}{technical}")
        set_label(self.usage, usage_text(part),
                  f"Utilización del sistema de archivos {part.name}\n"
                  f"Montajes: {', '.join(part.mountpoints)}\n{usage_text(part)}\n{state_detail(state, now)}")
        self.usage_bar.set_visible(True)
        update_bar(self.usage_bar, part.usage_percent, medium, high)
        if state.stale(now):
            self.usage_bar.add_css_class("storage-stale")
            self.usage_warning.set_visible(True)
            set_label(self.usage_warning, f"Uso obsoleto · {age_text(state, now)}", state_detail(state, now))

    def update(self, disk, alias, snap, now, unit, medium, high):
        model = (disk.model or "").strip()
        has_model = bool(model and model != "N/D")
        # Alias configuration and identity remain intact; the title identifies
        # the physical hardware, with its technical name as the fallback.
        set_label(self.title, model if has_model else disk.name)
        transport = f" · {disk.transport}" if disk.transport != "N/D" else ""
        set_label(self.technical, f"{disk.kind} · {disk.name} · {fmt_bytes(disk.size_bytes).replace('.', ',')}{transport}")
        self.model.set_visible(False)
        set_label(self.model, model or "")
        set_label(self.capacity, fmt_bytes(disk.size_bytes).replace('.', ','))
        set_label(self.device_summary, f"{disk.kind} · {disk.name}")
        topology = snap.sources.get("topology")
        temperature_key = disk.temperature_source or next((key for key in (
            "temperature:" + disk.name, "udisks:" + disk.name, "smart:" + disk.name
        ) if key in snap.sources), None)
        temperature = snap.sources.get(temperature_key)
        value = disk.temperature_c
        age = temperature.age(now) if temperature else None
        expired = temperature and (age is None or age > temperature.max_age)
        text = fmt_temp(None if expired else value, unit).replace('.', ',')
        if value is not None and temperature and temperature.error and not expired:
            text += " · anterior"
        if value is not None and temperature_key in {"udisks:" + disk.name, "smart:" + disk.name} and not expired:
            text += " · " + age_text(temperature, now)
        self.temperature.set_label(text)
        self.temperature.set_tooltip_text(f"Fuente: {temperature_key or 'N/D'}\nÚltima temperatura: {fmt_temp(value, unit)}\n{state_detail(temperature, now)}")
        usage_keys = {"usage:" + part.name for part in disk.partitions}
        if disk.direct_filesystem is not None:
            usage_keys.add("usage:" + disk.direct_filesystem.name)
        relevant = {key: state for key, state in snap.sources.items()
                    if key == "topology" or key in {"storage", "smart:" + disk.name, "temperature:" + disk.name, "udisks:" + disk.name}
                    or key in usage_keys}
        messages = []
        details = []
        def current_measurement(key):
            return not key.startswith(("smart:", "temperature:", "udisks:")) or key == temperature_key

        for key, state in relevant.items():
            details.append(f"{key}: {state_detail(state, now)}")
            if state.error:
                messages.append(brief_error(key, state.error))
            if current_measurement(key) and state.stale(now) and state.acquired_at is not None:
                messages.append("Datos obsoletos")
        for key, message in snap.errors.items():
            if key in relevant or key in {"topology", "storage", "smart:" + disk.name, "udisks:" + disk.name}:
                details.append(f"{key}: {message}")
                messages.append(brief_error(key, message))
        if value is None or expired:
            messages.append("Temperatura no disponible")
        stale_states = [state for key, state in relevant.items()
                        if current_measurement(key) and state.stale(now) and state.acquired_at is not None]
        if stale_states:
            messages.append(age_text(max(stale_states, key=lambda state: state.age(now)), now))
        if not messages:
            usage_states = [state for key, state in relevant.items() if key.startswith("usage:") and state.acquired_at is not None]
            state = max(usage_states, key=lambda state: state.age(now)) if usage_states else topology
            messages.append("Actualizado" + (" · " + age_text(state, now) if state else ""))
        set_label(self.status, " · ".join(dict.fromkeys(messages)), "\n".join(details))
        self.technical.set_tooltip_text(f"Capacidad/topología: {state_detail(topology, now)}\n{self.technical.get_label()}")
        self.capacity.set_tooltip_text(self.technical.get_tooltip_text())
        partitions = display_partitions(disk.partitions)
        volumes = [disk.direct_filesystem] if disk.direct_filesystem is not None else partitions
        summaries = []
        for part in volumes:
            mount = ", ".join(part.mountpoints) or "Sin montar"
            summaries.append(f"{partition_title(part)} · {part.fstype} · {mount}")
        mounted = sum(any(path.startswith("/") for path in part.mountpoints) for part in volumes)
        count = len(volumes)
        if count:
            mounting = f"{mounted} {'montado' if mounted == 1 else 'montados'}" if mounted else "Sin montar"
            text = f"{count} {'volumen' if count == 1 else 'volúmenes'} · {mounting}"
        else:
            text = "Sin particiones"
        set_label(self.partition_summary, text, "\n".join(summaries) or text)
        self._update_usage(partitions, snap, now, medium, high, disk.direct_filesystem)
        self.empty.set_visible(not partitions)
        active = set()
        previous = self.empty
        for position, part in enumerate(partitions):
            # Labels can be duplicated; technical identity determines row reuse.
            key = (part.name, part.depth, sum(p.name == part.name and p.depth == part.depth for p in partitions[:position]))
            active.add(key)
            row = self.partition_rows.get(key)
            if row is None:
                row = self.partition_rows[key] = PartitionRow()
                self.partitions_box.append(row)
            self.partitions_box.reorder_child_after(row, previous)
            previous = row
            row.update(part, snap.sources.get("usage:" + part.name), now, medium, high)
        for key in set(self.partition_rows) - active:
            self.partitions_box.remove(self.partition_rows.pop(key))


class StoragePanel(MetricCard):
    def __init__(self, unit="C", medium=60, high=85):
        super().__init__("Almacenamiento")
        self.body.set_spacing(12)
        self.remove_css_class("hw-card")
        self.add_css_class("storage-panel")
        self.unit, self.medium, self.high = unit, medium, high
        self.disk_cards = {}
        self.expansion_state = {}
        self._expansion_keys = {}
        self._last_snapshot = None

    def _remember_expansion(self, key, card):
        expanded = card.expander.get_expanded()
        self.expansion_state[key] = expanded
        physical = self._expansion_keys.get(key)
        if physical is not None:
            self.expansion_state[physical] = expanded

    def reset(self, signature):
        if signature != self._signature:
            for key, card in self.disk_cards.items():
                self._remember_expansion(key, card)
            super().reset(signature)
            self.disk_cards.clear()
            self._expansion_keys.clear()

    def update(self, snap, now):
        if self._last_snapshot is not None and snap.sequence < self._last_snapshot.sequence:
            snap = self._last_snapshot
        else:
            self._last_snapshot = snap
        if not snap.disks:
            self.reset(("storage-empty",))
            error = snap.errors.get("topology") or snap.errors.get("storage")
            self.set_row("state", "Estado", "Almacenamiento no disponible" if error else "No se detectaron discos")
            self._rows["state"][1].set_tooltip_text(error)
            return
        self.reset(("storage",))
        presentation = snap.storage_presentation
        identities = dict(presentation.identities) if presentation else {}
        alias_error = presentation.state.error if presentation else None
        for key, card in self.disk_cards.items():
            self._remember_expansion(key, card)
        active = set()
        previous = None
        if alias_error:
            self.set_row("alias-error", "Alias", "Configuración o identidad no disponible")
            self._rows["alias-error"][1].set_tooltip_text(state_detail(presentation.state, now))
            previous = self._rows["alias-error"][0].get_parent().get_parent()
        elif "alias-error" in self._rows:
            container = self._rows.pop("alias-error")[0].get_parent().get_parent()
            self.body.remove(container)
        for position, disk in enumerate(snap.disks):
            identity = identities.get(disk.name, DiskIdentity(f"unverified:{snap.sequence}:{position}:{disk.name}"))
            key = identity.key
            active.add(key)
            card = self.disk_cards.get(key)
            if card is None:
                card = self.disk_cards[key] = DiskCard(key, self.expansion_state.get(
                    identity.expansion_key or key, False))
                self.body.append(card)
            self._expansion_keys[key] = identity.expansion_key
            self.body.reorder_child_after(card, previous)
            previous = card
            card.update(disk, identity.alias, snap, now, self.unit, self.medium, self.high)
            self._remember_expansion(key, card)
        for key in set(self.disk_cards) - active:
            card = self.disk_cards.pop(key)
            self._remember_expansion(key, card)
            self._expansion_keys.pop(key, None)
            self.body.remove(card)
        for key in list(self.expansion_state):
            if key.startswith(('presence:', 'unverified:')) and key not in active:
                self.expansion_state.pop(key)
