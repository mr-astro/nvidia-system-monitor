"""SMART measurements and validated smartctl JSON, independent of GTK."""
from dataclasses import dataclass
import json

from src.data.errors import SourceState


@dataclass(frozen=True)
class AtaAttribute:
    """Reported ATA values, retained without a vendor interpretation."""
    id: int
    name: str
    flags: int
    value: int
    worst: int
    threshold: int
    pretty: int
    pretty_unit: int


@dataclass(frozen=True)
class SmartMetrics:
    # These are independent measurements, never filesystem utilisation or a
    # synthetic remaining-life score. Unknown fields stay None/unknown.
    health: str = "unknown"
    percentage_used: int | None = None
    power_on_hours: int | None = None
    temperature_c: float | None = None
    critical_warning: int | None = None
    media_errors: int | None = None
    error_log_entries: int | None = None
    warnings: tuple[str, ...] = ()
    exit_status: int | None = None
    state: SourceState = SourceState()
    source: str | None = None
    available_spare: int | None = None
    unsafe_shutdowns: int | None = None
    interface_crc_errors: int | None = None
    ata_attributes: tuple[AtaAttribute, ...] = ()


_HEALTH_BITS = {
    3: "SMART indica fallo de salud",
    4: "Atributo prefail bajo el umbral",
    5: "Atributo bajo el umbral en el pasado",
    6: "El registro SMART contiene errores",
    7: "El registro de autopruebas contiene errores",
}


def parse_smart_json(text, exit_status):
    """Return a sample or raise ValueError; bits 0–2 remain acquisition errors.

    Read only smartctl's decoded standard fields. In particular ATA attribute
    IDs/names/raw values are not mapped to NVMe wear or power-on hours.
    """
    if type(exit_status) is not int or not 0 <= exit_status <= 255:
        raise ValueError("smartctl: código de salida inválido")
    try:
        data = json.loads(text)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("smartctl: JSON malformado") from exc
    if not isinstance(data, dict):
        raise ValueError("smartctl: se esperaba un objeto JSON")

    def obj(parent, key):
        value = parent.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"smartctl: objeto {key} inválido")
        return value

    def uint(parent, key, maximum=None):
        if key not in parent:
            return None
        value = parent[key]
        if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
            raise ValueError(f"smartctl: campo {key} inválido")
        return value

    def boolean(parent, key):
        if key not in parent:
            return None
        if type(parent[key]) is not bool:
            raise ValueError(f"smartctl: campo {key} inválido")
        return parent[key]

    meta = obj(data, "smartctl")
    reported = uint(meta, "exit_status", 255)
    if reported is None or reported != exit_status:
        raise ValueError("smartctl: exit_status JSON ausente o contradictorio")
    support = obj(data, "smart_support")
    if boolean(support, "available") is False or boolean(support, "enabled") is False:
        raise ValueError("smartctl: SMART no disponible o deshabilitado")
    passed = boolean(obj(data, "smart_status"), "passed")
    nvme = obj(data, "nvme_smart_health_information_log")
    protocol = obj(data, "device").get("protocol")
    if nvme and protocol not in (None, "NVMe"):
        raise ValueError("smartctl: protocolo contradictorio con datos NVMe")
    used = uint(nvme, "percentage_used", 255)  # NVMe may legitimately exceed 100%.
    critical = uint(nvme, "critical_warning", 255)
    hours = uint(obj(data, "power_on_time"), "hours")
    if hours is None:
        hours = uint(nvme, "power_on_hours")
    temperature = obj(data, "temperature").get("current")
    if temperature is not None:
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not -273.15 <= temperature <= 1000):
            raise ValueError("smartctl: temperatura inválida")
        temperature = float(temperature)
    media = uint(nvme, "media_errors")
    entries = uint(nvme, "num_err_log_entries")
    spare = uint(nvme, "available_spare", 100)
    unsafe = uint(nvme, "unsafe_shutdowns")
    warnings = [message for bit, message in _HEALTH_BITS.items() if exit_status & (1 << bit)]
    if passed is False and not exit_status & 8:
        warnings.append("SMART indica fallo de salud")
    if critical:
        warnings.append(f"Advertencia crítica NVMe: 0x{critical:02x}")
    if used is not None and used >= 100:
        warnings.append("Percentage Used NVMe ha alcanzado el 100% de resistencia estimada")
    if media:
        warnings.append(f"Errores de integridad de medios NVMe: {media}")
    if entries:
        warnings.append(f"Entradas del registro de errores NVMe: {entries}")
    if all(value is None for value in (passed, used, hours, temperature, critical, media, entries, spare, unsafe)) and not exit_status & 248:
        raise ValueError("smartctl: sin atributos SMART disponibles")
    health = "failed" if passed is False or exit_status & 8 or critical else "passed" if passed is True else "unknown"
    return SmartMetrics(health, used, hours, temperature, critical, media, entries,
                        tuple(warnings), exit_status, source="smartctl",
                        available_spare=spare, unsafe_shutdowns=unsafe)
