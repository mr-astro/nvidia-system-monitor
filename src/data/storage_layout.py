"""Validated physical device extents, complementary to filesystem telemetry.

All offsets are bytes and ends are exclusive. lsblk START is always in 512-byte
sectors (util-linux misc-utils/lsblk.c, COL_START), *not* LOG-SEC units. Gaps mean
outside partition extents, not usable free space: they may contain table metadata.
"""
from dataclasses import dataclass

from src.data.errors import SourceState
from src.data.identity import Presence
from src.data.storage_order import attachment_matches


def integer(value, minimum=0):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= minimum else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value) if int(value) >= minimum else None
    return None


@dataclass(frozen=True)
class PartitionIdentity:
    name: str
    partuuid: str | None = None
    parttype: str | None = None
    number: int | None = None


@dataclass(frozen=True)
class PhysicalRegion:
    kind: str
    start_bytes: int
    end_bytes: int
    partition: PartitionIdentity | None = None

    @property
    def size_bytes(self):
        return self.end_bytes - self.start_bytes


@dataclass(frozen=True)
class PhysicalLayout:
    disk_name: str
    identity: Presence | None
    capacity_bytes: int | None
    logical_sector_bytes: int | None = None
    regions: tuple[PhysicalRegion, ...] = ()
    containers: tuple[PhysicalRegion, ...] = ()
    state: str = 'unavailable'
    reason: str | None = None
    source: SourceState = SourceState()

    @property
    def available(self):
        return self.state in {'partitioned', 'empty', 'whole_device'} and self.reason is None

    @property
    def unpartitioned_bytes(self):
        if not self.available:
            return None
        return sum(region.size_bytes for region in self.regions if region.kind == 'unpartitioned')

    def current(self, now):
        return self.available and self.source.acquired_at is not None and not self.source.stale(now)


def build_layout(node, presence, source=SourceState()):
    """Reject ambiguous input atomically; never return a partial plausible chart."""
    capacity = integer(node.get('size'), 1)
    sector = integer(node.get('log-sec'), 512)
    base = dict(disk_name=node['name'], identity=presence, capacity_bytes=capacity,
                logical_sector_bytes=sector, source=source)
    def unavailable(reason):
        return PhysicalLayout(**base, reason=reason)
    if node.get('type') != 'disk' or capacity is None or capacity % 512:
        return unavailable('Capacidad física ausente o no válida')
    if not attachment_matches(node, presence):
        return unavailable('Identidad o sesión de topología no verificable')
    if any(node.get(field) is not None and not isinstance(node[field], str) for field in ('pttype', 'kname')):
        return unavailable('Metadatos de tabla o identidad de kernel no válidos')
    if node.get('log-sec') is not None and (sector is None or sector & (sector - 1) or capacity % sector):
        return unavailable('Tamaño de sector lógico no válido')
    parts, unsupported = [], []
    def visit(parent, physical=True):
        for child in parent.get('children') or []:
            if child.get('type') == 'part':
                if not physical:
                    unsupported.append(child['name'])
                parts.append((child, parent))
            visit(child, physical and child.get('type') == 'part')
    visit(node)
    if unsupported:
        return unavailable('Tabla de particiones anidada en un dispositivo lógico')
    if not parts:
        if 'fstype' not in node or 'pttype' not in node:
            return unavailable('No se puede distinguir disco vacío de metadatos incompletos')
        if node.get('pttype') not in {None, '', 'gpt', 'dos'}:
            return unavailable('Tipo de tabla de particiones no compatible')
        if node.get('fstype'):
            if node.get('pttype'):
                return unavailable('Filesystem directo y tabla de particiones contradictorios')
            kind, state = 'whole_device', 'whole_device'
        elif node.get('children'):
            return unavailable('Capas lógicas sin respaldo físico identificable')
        elif any(isinstance(path, str) and path.startswith('/') for path in
                 (node.get('mountpoints') or [node.get('mountpoint')])):
            return unavailable('Montaje directo sin tipo de filesystem verificable')
        else:
            kind, state = 'unpartitioned', 'empty'
        return PhysicalLayout(**base, regions=(PhysicalRegion(kind, 0, capacity),), state=state)
    table = str(node.get('pttype') or '').lower()
    if table not in {'gpt', 'dos'}:
        return unavailable('Tipo de tabla de particiones ausente o no compatible')
    if node.get('fstype'):
        return unavailable('Sistema de archivos directo y particiones físicas contradictorios')
    names, uuids, numbers, extents, extended = set(), set(), set(), [], []
    disk_names = {node['name'], node.get('kname')}
    for part, parent in parts:
        if any(part.get(field) is not None and not isinstance(part[field], str) for field in ('kname', 'parttype')):
            return unavailable('Metadatos de partición no válidos')
        if part['name'] in names:
            return unavailable('Partición física repetida o identidad ambigua')
        names.add(part['name'])
        uuid = part.get('partuuid') or None
        if uuid is not None and not isinstance(uuid, str):
            return unavailable('PARTUUID no válido')
        if uuid and uuid.lower() in uuids:
            return unavailable('PARTUUID duplicado')
        if uuid:
            uuids.add(uuid.lower())
        start, size = integer(part.get('start')), integer(part.get('size'), 1)
        if start is None or size is None:
            return unavailable('Inicio o tamaño de partición ausente o no válido')
        start *= 512
        end = start + size
        alignment = sector or 512
        if size % alignment or start % alignment or end > capacity:
            return unavailable('Partición fuera de límites o no alineada al sector lógico')
        if part.get('log-sec') is not None and integer(part['log-sec'], 512) != alignment:
            return unavailable('Sector lógico contradictorio entre disco y partición')
        allowed_parents = disk_names | {parent['name'], parent.get('kname')}
        if part.get('pkname') and (not isinstance(part['pkname'], str) or part['pkname'] not in allowed_parents):
            return unavailable('PKNAME contradice la relación padre-hijo')
        parttype = str(part.get('parttype') or '').lower()
        number = integer(part.get('partn'), 1)
        if part.get('partn') is not None and (number is None or number in numbers):
            return unavailable('Número de partición no válido o duplicado')
        if number is not None:
            numbers.add(number)
        if table == 'dos' and not parttype:
            return unavailable('PARTTYPE necesario para distinguir contenedores DOS')
        try:
            code = int(parttype, 16) if table == 'dos' else None
            if code is not None and not 0 <= code <= 255:
                return unavailable('Tipo de partición DOS fuera de rango')
            is_extended = code in {0x05, 0x0f, 0x85}
        except ValueError:
            return unavailable('Tipo de partición DOS no válido')
        identity = PartitionIdentity(part['name'], uuid, parttype or None, number)
        region = PhysicalRegion('partition', start, end, identity)
        (extended if is_extended else extents).append(region)
        if parent is not node and parent.get('type') == 'part':
            parent_type = str(parent.get('parttype') or '').lower()
            if parent_type not in {'5', '05', '0x5', '0x05', 'f', '0f', '0xf', '0x0f', '85', '0x85'}:
                return unavailable('Partición física anidada sin contenedor extendido verificable')
    if len(extended) > 1:
        return unavailable('Más de un contenedor extendido')
    container = extended[0] if extended else None
    primary, logical = [], []
    for region in extents:
        number = region.partition.number
        if table == 'dos' and number is not None and number >= 5:
            if not container or not (container.start_bytes <= region.start_bytes < region.end_bytes <= container.end_bytes):
                return unavailable('Partición lógica fuera del contenedor extendido verificable')
            logical.append(region)
        else:
            if container and number is None:
                return unavailable('PARTN necesario para distinguir particiones lógicas')
            primary.append(region)
    if container:
        primary.append(container)
    def fill(regions, start, end, gap_kind):
        output, cursor = [], start
        for region in sorted(regions, key=lambda r: r.start_bytes):
            if region.start_bytes < cursor or region.end_bytes > end:
                raise ValueError('Regiones físicas solapadas o fuera de su contenedor')
            if region.start_bytes > cursor:
                output.append(PhysicalRegion(gap_kind, cursor, region.start_bytes))
            output.append(region)
            cursor = region.end_bytes
        if cursor < end:
            output.append(PhysicalRegion(gap_kind, cursor, end))
        return output
    try:
        regions = fill(primary, 0, capacity, 'unpartitioned')
        if container:
            inside = fill(logical, container.start_bytes, container.end_bytes, 'extended_reserved')
            regions = [child for region in regions for child in (inside if region is container else [region])]
    except ValueError as exc:
        return unavailable(str(exc))
    if sum(region.size_bytes for region in regions) != capacity:
        return unavailable('La distribución no coincide con la capacidad física')
    return PhysicalLayout(**base, regions=tuple(regions), containers=tuple(extended), state='partitioned')
