"""Explicit release identities. Importable before loading PyGObject or GTK."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    name: str
    application_id: str
    title: str
    directory_name: str
    logger_name: str

    @property
    def desktop_id(self) -> str:
        return self.application_id + ".desktop"


STABLE = Channel("stable", "cl.mastro.NvidiaSystemMonitor", "NVIDIA System Monitor",
                 "nvidia-system-monitor", "nvidia-system-monitor")
DEV = Channel("dev", "cl.mastro.NvidiaSystemMonitor.Dev", "NVIDIA System Monitor DEV",
              "nvidia-system-monitor-dev", "nvidia-system-monitor-dev")
DEFAULT_CHANNEL = "stable"


def get_channel(name: str = DEFAULT_CHANNEL) -> Channel:
    if name == "stable":
        return STABLE
    if name == "dev":
        return DEV
    raise ValueError(f"Canal no válido: {name!r}; use dev o stable")


def parse_channel_args(argv):
    """Consume only --channel, preserving GTK arguments and the -- delimiter."""
    remaining = [argv[0]]
    channel = DEFAULT_CHANNEL
    specified = False
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            remaining.extend(argv[index:])
            break
        if argument == "--channel" or argument.startswith("--channel="):
            if specified:
                raise ValueError("Especifique --channel solo una vez")
            specified = True
            if argument == "--channel":
                index += 1
                if index == len(argv):
                    raise ValueError("--channel requiere dev o stable")
                channel = argv[index]
            else:
                channel = argument.split("=", 1)[1]
            get_channel(channel)
        else:
            remaining.append(argument)
        index += 1
    return get_channel(channel), remaining
