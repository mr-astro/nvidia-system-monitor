"""Synthetic smartctl -j responses; never contact real block devices."""
import json


def smart_json(temperature=38, exit_status=0, **fields):
    data = {"smartctl": {"exit_status": exit_status},
            "smart_status": {"passed": not bool(exit_status & 8)},
            "temperature": {"current": temperature}}
    data.update(fields)
    return json.dumps(data)
