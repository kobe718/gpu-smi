# GPU-SMI (Windows)

A lightweight Windows GPU monitoring tool based on Windows Performance Counters and WMI. No vendor CLI tools required.

## Features

- ✅ Display GPU engine utilization
- ✅ Display dedicated/shared/total GPU memory usage
- ✅ Linux SMI-like output style
- ✅ Support all Windows GPUs (AMD, NVIDIA, Intel, etc.)
- ✅ No external dependencies, uses only Python standard library
- ✅ Continuous monitoring mode
- ✅ Verbose mode with process information

## Requirements

- Windows 10/11 or Windows Server 2016+
- Python 3.6+
- PowerShell (built-in on Windows)

## Installation

No additional dependencies required. Simply download `gpu-smi.py` and run.

```bash
# Clone or download the project
git clone <repository-url>
cd gpu-smi

# Or download the single file directly
curl -O gpu-smi.py
```

## Usage

### Basic Usage

```bash
# Show all GPUs
python gpu-smi.py

# Show a specific GPU (index starts from 0)
python gpu-smi.py -i 0

# Refresh every 5 seconds
python gpu-smi.py -l 5

# Show detailed information (including process list)
python gpu-smi.py -v

# Combine options
python gpu-smi.py -i 0 -l 2 -v
```

### Command Line Arguments

| Argument | Description |
|----------|-------------|
| `-i, --gpu INDEX` | Specify GPU index to display (default: all) |
| `-l, --loop SECONDS` | Set refresh interval in seconds |
| `-v, --verbose` | Show detailed information (including GPU process list) |
| `--version` | Display version information |
| `-h, --help` | Show help message |

## Output Example

```
======================================================================
GPU-SMI (Windows) - 2025-03-08 14:30:15
======================================================================

======================================================================
GPU 0: AMD Radeon RX 6800 XT
======================================================================

GPU Memory:
  Dedicated GPU Memory:
    Total:      16.00 GiB
    Used:       8.52 GiB (53.3%)
    Available:  7.48 GiB
    [####################--------------------] 53.3%
  Shared GPU Memory:
    Total:      16.00 GiB
    Used:       2.15 GiB (13.4%)
    Available:  13.85 GiB
  Total GPU Memory:
    Total:      32.00 GiB
    Used:       10.67 GiB (33.3%)
    Available:  21.33 GiB
    [#############---------------------------] 33.3%

GPU Engine Utilization:
  3D        :  45.2%
  Copy      :   2.1%
  Compute   :  78.5%
  VideoEncode:  12.3%
```

## Technical Implementation

This tool uses the following Windows native APIs to retrieve GPU information:

- **WMI (`Win32_VideoController`)**: GPU device information and basic properties
- **Performance Counters**:
  - `GPU Engine`: Engine utilization
  - `GPU Adapter Memory`: Dedicated/shared memory usage
  - `GPU Local/Non-Local Adapter Memory`: Local/non-local memory usage

### Memory Statistics

The tool provides three memory views:

1. **Dedicated GPU Memory**: GPU's dedicated VRAM
2. **Shared GPU Memory**: System memory shared with the GPU
3. **Total GPU Memory**: Total available GPU memory (dedicated + shared)

For integrated GPUs (UMA architecture), total memory may match the physical system memory.

## Notes

1. **Permissions**: Administrator privileges may be required to access certain performance counters
2. **WMI AdapterRAM Limitation**: For VRAM over 4GB, WMI's `AdapterRAM` field may be inaccurate (uint32 limitation)
3. **Integrated GPUs**: For UMA devices, memory statistics may include shared system memory
4. **Performance Overhead**: The tool uses PowerShell queries; refresh interval should not be less than 1 second

## Known Limitations

- Process-level GPU usage monitoring is a simplified implementation with limited accuracy
- Some older Windows versions or GPU drivers may not support all performance counters
- Temperature and power consumption not yet implemented (requires vendor-specific APIs)

## Comparison with Vendor Tools

| Feature | gpu-smi.py (this tool) | AMD SMI | NVIDIA SMI |
|---------|----------------------|---------|------------|
| Cross-vendor Support | ✅ All GPUs | ❌ AMD only | ❌ NVIDIA only |
| External Dependencies | ❌ None | ✅ Required | ✅ Required |
| Temperature Monitoring | ❌ | ✅ | ✅ |
| Power Monitoring | ❌ | ✅ | ✅ |
| Process-level Precision | Simplified | ✅ | ✅ |
| Windows Native | ✅ | ❌ | ✅ |

## Troubleshooting

### Issue: "No GPU devices found"

**Solutions**:
- Ensure the system has physical GPU devices
- Check if GPU drivers are correctly installed
- Try running with administrator privileges

### Issue: Memory shows 0 or inaccurate values

**Solutions**:
- Some counters require the GPU to be in use to display values
- Check if Windows Performance Counter service is running
- Review the notes in the output for data source information

### Issue: PowerShell execution errors

**Solutions**:
- Ensure PowerShell is available (`powershell -Command "Get-Date"`)
- Check PowerShell execution policy settings

## Contributing

Issues and Pull Requests are welcome!

## License

MIT License

## Acknowledgments

Inspired by Linux's `nvidia-smi` and AMD's `rocm-smi` tools.
