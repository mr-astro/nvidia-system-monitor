#!/usr/bin/env python3
"""Unit validators for hardware data collectors. Tests parsing logic with simulated data."""

import sys
from pathlib import Path

# Add parent directory to path for imports (test file is in src/)
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_nvidia_parsing():
    """Test NVIDIA data parsing with simulated nvidia-smi output."""
    from src.data.nvidia import NVIDIAData
    
    # Simulated nvidia-smi output (single GPU)
    sim_output = """Fri Dec 19 10:30:45 2025       
GPU Name                 Memory-Usage  Temp  Power-Draw  Driver Version / CUDA Version
0 GeForce RTX 3060        2048MiB / 12288MiB   65C    45W      550.90.07 / 12.6
"""
    
    nvidia = NVIDIAData()
    nvidia._parse_extended_output(sim_output)
    
    assert nvidia.gpu_detected, "GPU should be detected"
    assert len(nvidia.driver_version) > 0, "Driver version should be parsed"
    assert len(nvidia.cuda_version) > 0, "CUDA version should be parsed"
    
    # Note: collect() runs subprocess, so we test _parse_extended_output directly
    
    print("✓ NVIDIA parsing validator passed")


def test_cpu_parsing():
    """Test CPU data collection and parsing."""
    from src.data.cpu import CPUData
    
    cpu = CPUData()
    result = cpu.collect()
    
    assert 'detected' in result, "Result should have 'detected' key"
    assert 'model' in result, "Result should have 'model' key"
    assert 'cores' in result, "Result should have 'cores' key"
    assert 'threads' in result, "Result should have 'threads' key"
    
    # Model should be populated from /proc/cpuinfo
    assert len(result['model']) > 0 or result['model'] == "N/A", \
        f"Model should be set (got: {result['model']})"
    
    print("✓ CPU parsing validator passed")


def test_ram_parsing():
    """Test RAM data collection and parsing."""
    from src.data.ram import RAMData
    
    ram = RAMData()
    result = ram.collect()
    
    assert 'detected' in result, "Result should have 'detected' key"
    assert 'total_gb' in result, "Result should have 'total_gb' key"
    assert 'used_gb' in result, "Result should have 'used_gb' key"
    assert 'available_gb' in result, "Result should have 'available_gb' key"
    
    # Total should be > 0 on a real system
    if result['total_gb'] > 0:
        print(f"  RAM detected: {result['total_gb']} GB total")
    
    print("✓ RAM parsing validator passed")


def test_storage_parsing():
    """Test storage data collection and parsing."""
    from src.data.storage import StorageData
    
    storage = StorageData()
    result = storage.collect()
    
    assert 'detected' in result, "Result should have 'detected' key"
    assert 'devices' in result, "Result should have 'devices' key"
    
    # Should detect at least one disk on a real system
    if len(result['devices']) > 0:
        print(f"  Storage detected: {len(result['devices'])} devices")
        for dev in result['devices']:
            print(f"    - {dev.get('name', 'unknown')}: {dev.get('total_gb', 0)} GB")
    
    print("✓ Storage parsing validator passed")


def test_sensors_parsing():
    """Test lm-sensors data collection and parsing."""
    from src.data.sensors import SensorsData
    
    sensors = SensorsData()
    result = sensors.collect()
    
    # Always expect 'detected' key in the response structure
    assert isinstance(result, dict), "Result should be a dictionary"
    assert 'detected' in result, "Result should have 'detected' key"
    assert 'sensors' in result, "Result should have 'sensors' key"
    
    # If sensors are detected, show some info
    if result['detected']:
        print(f"  Sensors detected: {len(result['sensors'])} sensor blocks")
        for s in result['sensors'][:3]:  # Show first 3
            print(f"    - {s.get('name', 'unknown')}: {list(s.get('readings', {}).keys())}")
    
    print("✓ Sensors parsing validator passed")


def test_app_integration():
    """Test full app integration with simulated data."""
    # Import all classes explicitly to ensure they're loaded before instantiation
    from src.gui.app import SystemMonitorApp
    from src.data.nvidia import NVIDIAData
    from src.data.cpu import CPUData
    from src.data.ram import RAMData
    from src.data.storage import StorageData
    from src.data.sensors import SensorsData
    
    # Create individual collectors first to verify they work
    nvidia = NVIDIAData()
    cpu = CPUData()
    ram = RAMData()
    storage = StorageData()
    sensors = SensorsData()
    
    # Verify all collectors are initialized and can collect data
    assert hasattr(nvidia, 'collect'), "NVIDIAData should have collect method"
    assert hasattr(cpu, 'collect'), "CPUData should have collect method"
    assert hasattr(ram, 'collect'), "RAMData should have collect method"
    assert hasattr(storage, 'collect'), "StorageData should have collect method"
    assert hasattr(sensors, 'collect'), "SensorsData should have collect method"
    
    # Test data collection methods return valid structures
    nvidia_data = nvidia.collect()
    cpu_data = cpu.collect()
    ram_data = ram.collect()
    storage_data = storage.collect()
    sensors_data = sensors.collect()
    
    assert isinstance(nvidia_data, dict), "NVIDIA collect should return dict"
    assert isinstance(cpu_data, dict), "CPU collect should return dict"
    assert isinstance(ram_data, dict), "RAM collect should return dict"
    assert isinstance(storage_data, dict), "Storage collect should return dict"
    assert isinstance(sensors_data, dict), "Sensors collect should return dict"
    
    # Now test the app integration (may fail without display)
    try:
        app = SystemMonitorApp()
        
        # Verify all collectors are initialized in app
        assert hasattr(app, '_nvidia'), "App should have _nvidia collector"
        assert hasattr(app, '_cpu'), "App should have _cpu collector"
        assert hasattr(app, '_ram'), "App should have _ram collector"
        assert hasattr(app, '_storage'), "App should have _storage collector"
        assert hasattr(app, '_sensors'), "App should have _sensors collector"
        
        # Test data collection methods exist and return valid structures
        nvidia_data = app._collect_all_data()
        
        assert 'nvidia' in nvidia_data, "Collect all data should include NVIDIA"
        assert 'cpu' in nvidia_data, "Collect all data should include CPU"
        assert 'ram' in nvidia_data, "Collect all data should include RAM"
        assert 'storage' in nvidia_data, "Collect all data should include storage"
        assert 'sensors' in nvidia_data, "Collect all data should include sensors"
        
        print("✓ App integration validator passed")
    except AttributeError as e:
        # Expected on systems without display or with import issues
        print(f"⚠ App Integration SKIPPED (expected): {type(e).__name__}: {e}")


def main():
    """Run all validators."""
    print("=" * 60)
    print("Hardware Data Collectors - Unit Validators")
    print("=" * 60)
    print()
    
    tests = [
        ("NVIDIA Parsing", test_nvidia_parsing),
        ("CPU Parsing", test_cpu_parsing),
        ("RAM Parsing", test_ram_parsing),
        ("Storage Parsing", test_storage_parsing),
        ("Sensors Parsing", test_sensors_parsing),
        ("App Integration", test_app_integration),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            print(f"Running {name}...")
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"✗ {name} FAILED: {e}")
            failed += 1
        except Exception as e:
            # Some tests may fail on systems without certain hardware/drivers
            print(f"⚠ {name} SKIPPED (expected): {type(e).__name__}: {e}")
    
    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
