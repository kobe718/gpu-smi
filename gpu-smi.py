#!/usr/bin/env python3
"""
gpu-smi.py - Simple Windows GPU monitoring tool
Based on Windows Performance Counters and WMI (no vendor CLI required)

Features:
- Show GPU engine utilization
- Show actual GPU memory usage
- Use an output style similar to Linux SMI tools
"""

import argparse
import ctypes
import io
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Any, TextIO, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict


@dataclass
class GPUProcessInfo:
    """GPU process information"""

    pid: int
    process_name: str
    gpu_usage: float
    mem_usage: float


@dataclass
class GPUEngineInfo:
    """GPU engine information"""

    name: str
    utilization: float  # 0-100%


@dataclass
class GPUMemoryStats:
    """GPU memory statistics (bytes)"""

    total_bytes: float
    used_bytes: float
    source: str

    @property
    def free_bytes(self) -> float:
        return max(self.total_bytes - self.used_bytes, 0.0)

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(max(self.used_bytes / self.total_bytes * 100.0, 0.0), 100.0)


@dataclass
class GPUInfo:
    """GPU information"""

    index: int
    name: str
    luid: str
    dedicated_memory: GPUMemoryStats
    shared_memory: GPUMemoryStats
    total_memory: GPUMemoryStats
    engines: List[GPUEngineInfo]
    temperature: Optional[float] = None
    power_draw: Optional[float] = None
    processes: List[GPUProcessInfo] = field(default_factory=list)
    memory_notes: List[str] = field(default_factory=list)
    legacy_adapter_ram_bytes: Optional[float] = None


class WindowsGPUMonitor:
    """Generic Windows GPU monitor (WMI + Performance Counters)."""

    def __init__(self):
        self._gpu_adapters = []
        self._memory_instance_map: Dict[int, str] = {}
        self._total_system_memory_bytes = self._get_total_system_memory_bytes()
        self._discover_gpus()

    @staticmethod
    def _get_total_system_memory_bytes() -> float:
        """Get total physical system memory (bytes)."""
        if sys.platform != "win32":
            return 0.0

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)

        try:
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return 0.0
            return float(stat.ullTotalPhys)
        except Exception:
            return 0.0

    @staticmethod
    def _extract_number_recursive(data: Any, target_keys: List[str]) -> Optional[float]:
        """Recursively extract a numeric field from nested JSON data."""
        if isinstance(data, dict):
            for key, value in data.items():
                key_norm = key.lower().replace("-", "_")
                if any(target in key_norm for target in target_keys):
                    if isinstance(value, (int, float)):
                        return float(value)
                    if isinstance(value, str):
                        num_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
                        if num_match:
                            return float(num_match.group(1))
                nested = WindowsGPUMonitor._extract_number_recursive(value, target_keys)
                if nested is not None:
                    return nested
        elif isinstance(data, list):
            for item in data:
                nested = WindowsGPUMonitor._extract_number_recursive(item, target_keys)
                if nested is not None:
                    return nested
        return None

    @staticmethod
    def _is_hardware_gpu(name: str, pnp_device_id: str) -> bool:
        """Filter out obvious software, remote, and placeholder display adapters."""
        name_upper = name.upper()
        pnp_upper = pnp_device_id.upper()

        if "MICROSOFT BASIC" in name_upper or "REMOTE DISPLAY" in name_upper:
            return False
        if pnp_upper and not pnp_upper.startswith("PCI\\"):
            return False
        return True

    def _discover_gpus(self):
        """Discover GPUs in the system using WMI."""
        try:
            # Use CIM to get video controller information (faster than classic WMI)
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DeviceID, DriverVersion, VideoProcessor, PNPDeviceID | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8",
                errors="ignore",
            )

            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)

                # Handle either a list or a single object
                if isinstance(data, dict):
                    data = [data]

                for i, gpu in enumerate(data):
                    name = gpu.get("Name", "Unknown GPU")
                    adapter_ram = gpu.get("AdapterRAM", 0)
                    device_id = gpu.get("DeviceID", f"GPU{i}")
                    pnp_device_id = gpu.get("PNPDeviceID", "")

                    # Keep physical hardware GPUs from any vendor
                    if self._is_hardware_gpu(name, pnp_device_id):
                        self._gpu_adapters.append(
                            {
                                "name": name,
                                "device_id": device_id,
                                "adapter_ram_bytes": float(adapter_ram)
                                if adapter_ram
                                else 0.0,
                                "pnp_device_id": pnp_device_id,
                                "index": len(self._gpu_adapters),
                            }
                        )

        except Exception as e:
            print(f"Error: failed to discover GPUs - {e}", file=sys.stderr)

    @staticmethod
    def _normalize_instance_key(instance: str) -> str:
        """Normalize a performance counter instance name to adapter scope."""
        match = re.search(r"(luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", instance)
        if match:
            return match.group(1).lower()
        return instance.lower()

    def _read_gpu_memory_counters(self) -> Dict[str, Dict[str, float]]:
        """
        Read and aggregate GPU memory counters in one shot.

        Preferred counters:
        - Dedicated Usage / Shared Usage: current dedicated/shared GPU memory usage
        - Dedicated Limit / Shared Limit: driver-exposed limits, if available
        - Local Usage / Non-Local Usage: fallback usage metrics
        """
        counters_by_key: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {
                "dedicated_used_bytes": 0.0,
                "shared_used_bytes": 0.0,
                "committed_bytes": 0.0,
                "local_used_bytes": 0.0,
                "non_local_used_bytes": 0.0,
                "dedicated_limit_bytes": 0.0,
                "shared_limit_bytes": 0.0,
            }
        )

        ps_script = r"""
        $counterPaths = @(
          "\GPU Adapter Memory(*)\Dedicated Usage",
          "\GPU Adapter Memory(*)\Shared Usage",
          "\GPU Adapter Memory(*)\Total Committed",
          "\GPU Adapter Memory(*)\Dedicated Limit",
          "\GPU Adapter Memory(*)\Shared Limit",
          "\GPU Local Adapter Memory(*)\Local Usage",
          "\GPU Non Local Adapter Memory(*)\Non-Local Usage"
        )

        $samples = @()
        foreach ($p in $counterPaths) {
          try {
            $counter = Get-Counter $p -ErrorAction Stop
            $samples += $counter.CounterSamples
          } catch {
          }
        }

        $result = @()
        foreach ($sample in $samples) {
          $path = $sample.Path
          if ($path -match "\\GPU Adapter Memory\(([^)]+)\)\\Dedicated Usage") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "dedicated_used_bytes"
              Value = [double]$sample.CookedValue
            }
          } elseif ($path -match "\\GPU Adapter Memory\(([^)]+)\)\\Shared Usage") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "shared_used_bytes"
              Value = [double]$sample.CookedValue
            }
          } elseif ($path -match "\\GPU Adapter Memory\(([^)]+)\)\\Total Committed") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "committed_bytes"
              Value = [double]$sample.CookedValue
            }
          } elseif ($path -match "\\GPU Adapter Memory\(([^)]+)\)\\Dedicated Limit") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "dedicated_limit_bytes"
              Value = [double]$sample.CookedValue
            }
          } elseif ($path -match "\\GPU Adapter Memory\(([^)]+)\)\\Shared Limit") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "shared_limit_bytes"
              Value = [double]$sample.CookedValue
            }
          } elseif ($path -match "\\GPU Local Adapter Memory\(([^)]+)\)\\Local Usage") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "local_used_bytes"
              Value = [double]$sample.CookedValue
            }
          } elseif ($path -match "\\GPU Non Local Adapter Memory\(([^)]+)\)\\Non-Local Usage") {
            $result += [PSCustomObject]@{
              Instance = $matches[1]
              Metric = "non_local_used_bytes"
              Value = [double]$sample.CookedValue
            }
          }
        }

        $result | ConvertTo-Json -Compress
        """

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=6,
                encoding="utf-8",
                errors="ignore",
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {}

            data: Any = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]

            if not isinstance(data, list):
                return {}

            for sample in data:
                instance = str(sample.get("Instance", ""))
                metric = str(sample.get("Metric", ""))
                value = float(sample.get("Value", 0.0))
                if not instance:
                    continue

                key = self._normalize_instance_key(instance)
                value = max(value, 0.0)
                if metric in {
                    "dedicated_used_bytes",
                    "shared_used_bytes",
                    "committed_bytes",
                    "local_used_bytes",
                    "non_local_used_bytes",
                }:
                    counters_by_key[key][metric] += value
                elif metric in {"dedicated_limit_bytes", "shared_limit_bytes"}:
                    counters_by_key[key][metric] = max(
                        counters_by_key[key][metric], value
                    )

            return dict(counters_by_key)
        except Exception as e:
            print(f"Warning: failed to read GPU memory counters - {e}", file=sys.stderr)
            return {}

    def _build_memory_instance_mapping(
        self, counters: Dict[str, Dict[str, float]]
    ) -> None:
        """Map GPU adapter indices to memory counter instances."""
        if self._memory_instance_map or not counters:
            return

        keys = list(counters.keys())
        adapter_count = len(self._gpu_adapters)
        if adapter_count == 0:
            return

        # Prefer instances with exposed limits; otherwise fall back to sorting by live usage
        keys_sorted = sorted(
            keys,
            key=lambda k: (
                counters[k].get("dedicated_limit_bytes", 0.0),
                counters[k].get("local_used_bytes", 0.0),
                counters[k].get("dedicated_used_bytes", 0.0),
                counters[k].get("shared_used_bytes", 0.0),
                counters[k].get("committed_bytes", 0.0),
            ),
            reverse=True,
        )

        # 1) If counts match, map directly by sorted position
        if len(keys_sorted) >= adapter_count:
            for idx in range(adapter_count):
                self._memory_instance_map[idx] = keys_sorted[idx]
            return

        # 2) If there are fewer counter instances than adapters, map what we can and let the rest fall back to WMI
        for idx, key in enumerate(keys_sorted):
            self._memory_instance_map[idx] = key

    def _is_likely_uma_adapter(self, adapter: Dict[str, Any]) -> bool:
        """Heuristically detect an integrated / UMA adapter."""
        adapter_ram_bytes = float(adapter.get("adapter_ram_bytes", 0.0) or 0.0)
        name = str(adapter.get("name", "")).upper()

        if self._total_system_memory_bytes <= 0:
            return False

        return (
            "GRAPHICS" in name
            and adapter_ram_bytes > 0
            and adapter_ram_bytes <= 4.5 * (1024**3)
            and self._total_system_memory_bytes >= 8 * (1024**3)
        )

    def _get_gpu_memory_stats(self, index: int) -> Dict[str, Any]:
        """Get GPU memory statistics."""
        adapter = self._gpu_adapters[index]
        notes: List[str] = []

        counters = self._read_gpu_memory_counters()
        if counters:
            self._build_memory_instance_mapping(counters)
            key = self._memory_instance_map.get(index)

            # If no instance was mapped, fall back to ranking by live memory usage
            if not key:
                ranked = sorted(
                    counters.keys(),
                    key=lambda k: (
                        counters[k].get("dedicated_used_bytes", 0.0)
                        + counters[k].get("shared_used_bytes", 0.0)
                        + counters[k].get("local_used_bytes", 0.0)
                    ),
                    reverse=True,
                )
                if index < len(ranked):
                    key = ranked[index]

            if key and key in counters:
                sample = counters[key]
                dedicated_used = max(
                    sample.get("dedicated_used_bytes", 0.0),
                    sample.get("local_used_bytes", 0.0),
                    0.0,
                )
                shared_used = max(
                    sample.get("shared_used_bytes", 0.0),
                    sample.get("non_local_used_bytes", 0.0),
                    0.0,
                )

                dedicated_total = max(sample.get("dedicated_limit_bytes", 0.0), 0.0)
                shared_total = max(sample.get("shared_limit_bytes", 0.0), 0.0)
                adapter_ram_bytes = max(
                    float(adapter.get("adapter_ram_bytes", 0.0) or 0.0), 0.0
                )

                if shared_total <= 0 and self._total_system_memory_bytes > 0:
                    shared_total = self._total_system_memory_bytes / 2.0
                    notes.append(
                        "Shared GPU memory limit estimated as half of physical system memory based on Windows VidMm behavior."
                    )

                if dedicated_total <= 0:
                    if (
                        self._is_likely_uma_adapter(adapter)
                        and self._total_system_memory_bytes > 0
                    ):
                        dedicated_total = self._total_system_memory_bytes
                        notes.append(
                            "Integrated / UMA characteristics detected; dedicated GPU memory limit estimated from local memory segment behavior."
                        )
                    elif adapter_ram_bytes > 0:
                        dedicated_total = adapter_ram_bytes
                        notes.append(
                            "Dedicated GPU memory limit fell back to WMI AdapterRAM; this uint32 field may underreport values above 4 GiB."
                        )
                    else:
                        notes.append("Dedicated GPU memory limit was not available.")

                if dedicated_total > 0 and dedicated_used > dedicated_total:
                    dedicated_total = dedicated_used
                if shared_total > 0 and shared_used > shared_total:
                    shared_total = shared_used

                total_used = dedicated_used + shared_used
                total_total = (
                    dedicated_total + shared_total
                    if (dedicated_total > 0 or shared_total > 0)
                    else 0.0
                )

                if (
                    self._is_likely_uma_adapter(adapter)
                    and total_total > self._total_system_memory_bytes > 0
                ):
                    notes.append(
                        "On integrated / UMA devices, total GPU memory follows Windows graphics memory reporting and may exceed a single physical RAM/VRAM number."
                    )

                committed_bytes = max(sample.get("committed_bytes", 0.0), 0.0)
                if committed_bytes > total_used * 1.15 and total_used > 0:
                    notes.append(
                        "Total Committed exceeded Dedicated+Shared Usage; total usage defaults to the latter for display."
                    )
                elif total_used <= 0 and committed_bytes > 0:
                    total_used = committed_bytes

                return {
                    "dedicated_used_bytes": dedicated_used,
                    "dedicated_total_bytes": dedicated_total,
                    "shared_used_bytes": shared_used,
                    "shared_total_bytes": shared_total,
                    "total_used_bytes": total_used,
                    "total_total_bytes": total_total,
                    "legacy_adapter_ram_bytes": adapter_ram_bytes or None,
                    "notes": notes,
                }

        adapter_ram_bytes = max(
            float(adapter.get("adapter_ram_bytes", 0.0) or 0.0), 0.0
        )
        if adapter_ram_bytes > 0:
            notes.append(
                "No valid GPU memory counters were available; fell back to legacy WMI AdapterRAM semantics."
            )

        return {
            "dedicated_used_bytes": 0.0,
            "dedicated_total_bytes": adapter_ram_bytes,
            "shared_used_bytes": 0.0,
            "shared_total_bytes": self._total_system_memory_bytes / 2.0
            if self._total_system_memory_bytes > 0
            else 0.0,
            "total_used_bytes": 0.0,
            "total_total_bytes": adapter_ram_bytes
            + (
                self._total_system_memory_bytes / 2.0
                if self._total_system_memory_bytes > 0
                else 0.0
            ),
            "legacy_adapter_ram_bytes": adapter_ram_bytes or None,
            "notes": notes,
        }

    @staticmethod
    def _parse_engine_counter_path(path: str) -> Optional[Tuple[str, str, str, str]]:
        """Parse a GPU Engine counter path into adapter/engine identity."""
        match = re.search(
            r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_(\d+)_eng_(\d+)_engtype_([^)]+)",
            path,
            re.IGNORECASE,
        )
        if not match:
            return None

        luid_key = f"luid_{match.group(1).lower()}"
        physical_engine = match.group(2)
        engine_index = match.group(3)
        engine_name = re.sub(r"\s+", " ", match.group(4)).strip().lower()
        return luid_key, physical_engine, engine_index, engine_name

    def _read_gpu_engine_counter_samples(self) -> List[Dict[str, Any]]:
        """Read raw GPU engine performance counter samples."""
        ps_script = r"""
        $counter = Get-Counter "\GPU Engine(*)\Utilization Percentage" -ErrorAction SilentlyContinue
        if ($counter) {
          $counter.CounterSamples | Select-Object Path, CookedValue | ConvertTo-Json -Compress
        } else {
          "[]"
        }
        """

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8",
                errors="ignore",
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []

            data: Any = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]

            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"Warning: failed to read GPU engine counters - {e}", file=sys.stderr)
            return []

    def _get_gpu_engines_perf(self, index: Optional[int] = None) -> List[GPUEngineInfo]:
        """Get GPU engine utilization using Performance Counters."""
        target_luid = (
            self._memory_instance_map.get(index) if index is not None else None
        )
        engine_totals: Dict[Tuple[str, str, str, str], float] = defaultdict(float)

        for sample in self._read_gpu_engine_counter_samples():
            parsed = self._parse_engine_counter_path(str(sample.get("Path", "")))
            if not parsed:
                continue

            luid_key, physical_engine, engine_index, engine_name = parsed
            value = max(float(sample.get("CookedValue", 0.0)), 0.0)
            engine_totals[(luid_key, physical_engine, engine_index, engine_name)] += (
                value
            )

        if not engine_totals:
            return []

        for engine_key in list(engine_totals.keys()):
            engine_totals[engine_key] = min(engine_totals[engine_key], 100.0)

        selected_engines = engine_totals
        if target_luid:
            filtered_engines = {
                key: value
                for key, value in engine_totals.items()
                if key[0] == target_luid
            }
            if filtered_engines:
                selected_engines = filtered_engines

        engines_by_name: Dict[str, float] = {}
        for (_, _, _, engine_name), utilization in selected_engines.items():
            engines_by_name[engine_name] = max(
                engines_by_name.get(engine_name, 0.0), utilization
            )

        return [
            GPUEngineInfo(name=name, utilization=utilization)
            for name, utilization in sorted(engines_by_name.items())
        ]

    def _get_gpu_processes(self) -> List[GPUProcessInfo]:
        """Get a list of processes using the GPU."""
        processes = []

        try:
            # Use PowerShell to retrieve GPU-related processes
            result = subprocess.run(
                [
                    "powershell",
                    "-Command",
                    """
                 $processes = Get-WmiObject Win32_VideoController | ForEach-Object {
                     $controller = $_
                     Get-WmiObject Win32_Process | Where-Object {
                         # Simplified process detection
                         $true
                     } | Select-Object -First 10 ProcessId, Name
                 }
                 $processes | ConvertTo-Json
                 """,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8",
                errors="ignore",
            )

            # Simplified handling; real-world usage would need more accurate process-to-GPU correlation
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    seen_pids = set()
                    for proc in data[:10]:  # Limit output to the first 10 processes
                        pid = proc.get("ProcessId", 0)
                        if pid and pid not in seen_pids:
                            seen_pids.add(pid)
                            processes.append(
                                GPUProcessInfo(
                                    pid=int(pid),
                                    process_name=proc.get("Name", "Unknown"),
                                    gpu_usage=0.0,
                                    mem_usage=0.0,
                                )
                            )

        except Exception as e:
            print(f"Warning: failed to read GPU processes - {e}", file=sys.stderr)

        return processes

    def get_gpu_info(self, index: int = 0) -> Optional[GPUInfo]:
        """Get information for a specific GPU."""
        if index >= len(self._gpu_adapters):
            return None

        adapter = self._gpu_adapters[index]

        # Get memory information
        memory_stats = self._get_gpu_memory_stats(index)

        # Get engine information filtered to this GPU when possible
        engines = self._get_gpu_engines_perf(index)

        # Get process information
        processes = self._get_gpu_processes()

        return GPUInfo(
            index=index,
            name=adapter["name"],
            luid=adapter.get("device_id", f"GPU{index}"),
            dedicated_memory=GPUMemoryStats(
                total_bytes=float(memory_stats.get("dedicated_total_bytes", 0.0)),
                used_bytes=float(memory_stats.get("dedicated_used_bytes", 0.0)),
                source="windows_perf_counter",
            ),
            shared_memory=GPUMemoryStats(
                total_bytes=float(memory_stats.get("shared_total_bytes", 0.0)),
                used_bytes=float(memory_stats.get("shared_used_bytes", 0.0)),
                source="windows_vidmm",
            ),
            total_memory=GPUMemoryStats(
                total_bytes=float(memory_stats.get("total_total_bytes", 0.0)),
                used_bytes=float(memory_stats.get("total_used_bytes", 0.0)),
                source="windows_task_manager_like",
            ),
            engines=engines,
            processes=processes,
            memory_notes=list(memory_stats.get("notes", [])),
            legacy_adapter_ram_bytes=memory_stats.get("legacy_adapter_ram_bytes"),
        )

    def get_all_gpus(self) -> List[GPUInfo]:
        """Get information for all GPUs."""
        gpus = []
        for i in range(len(self._gpu_adapters)):
            gpu = self.get_gpu_info(i)
            if gpu:
                gpus.append(gpu)
        return gpus


def format_size_bytes(size_bytes: float) -> str:
    """Format a byte value as GiB/TiB."""
    gib = size_bytes / (1024**3)
    if gib >= 1024:
        return f"{gib / 1024:.2f} TiB"
    return f"{gib:.2f} GiB"


def parse_refresh_interval(value: str) -> int:
    """Parse and validate the refresh interval."""
    try:
        interval = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("refresh interval must be an integer") from exc

    if interval <= 0:
        raise argparse.ArgumentTypeError("refresh interval must be a positive integer")

    return interval


def configure_stdout() -> None:
    """Keep UTF-8 output enabled on Windows without buffering loop output."""
    if sys.platform != "win32" or not hasattr(sys.stdout, "buffer"):
        return

    reconfigure = getattr(sys.stdout, "reconfigure", None)

    try:
        if callable(reconfigure):
            reconfigure(
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
        else:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
    except Exception:
        return


def print_memory_block(
    title: str,
    stats: GPUMemoryStats,
    show_bar: bool = True,
    file: Optional[TextIO] = None,
):
    """Print a single GPU memory section."""
    stream = file if file is not None else sys.stdout

    print(f"  {title}:", file=stream)
    total_text = (
        format_size_bytes(stats.total_bytes) if stats.total_bytes > 0 else "Unknown"
    )
    print(f"    Total:      {total_text}", file=stream)
    print(
        f"    Used:       {format_size_bytes(stats.used_bytes)} ({stats.percent:.1f}%)",
        file=stream,
    )
    if stats.total_bytes > 0:
        print(f"    Available:  {format_size_bytes(stats.free_bytes)}", file=stream)
        if show_bar:
            bar_width = 40
            filled = int(stats.percent / 100 * bar_width)
            bar = "#" * filled + "-" * (bar_width - filled)
            print(f"    [{bar}] {stats.percent:.1f}%", file=stream)


def print_gpu_info(gpu: GPUInfo, verbose: bool = False, file: Optional[TextIO] = None):
    """Print GPU information."""
    stream = file if file is not None else sys.stdout

    print(f"\n{'=' * 70}", file=stream)
    print(f"GPU {gpu.index}: {gpu.name}", file=stream)
    print(f"{'=' * 70}", file=stream)

    # Memory information
    print("\nGPU Memory:", file=stream)
    print_memory_block("Dedicated GPU Memory", gpu.dedicated_memory, file=stream)
    print_memory_block(
        "Shared GPU Memory", gpu.shared_memory, show_bar=False, file=stream
    )
    print_memory_block("Total GPU Memory", gpu.total_memory, file=stream)

    # Engine utilization
    if gpu.engines:
        print("\nGPU Engine Utilization:", file=stream)
        for engine in sorted(gpu.engines, key=lambda item: item.name.casefold()):
            print(f"  {engine.name:20s}: {engine.utilization:6.1f}%", file=stream)

    # Process list
    if gpu.processes and verbose:
        print("\nGPU Processes:", file=stream)
        print(f"  {'PID':<10} {'Process Name':<30} {'GPU%':>10}", file=stream)
        print(f"  {'-' * 60}", file=stream)
        for proc in gpu.processes[:10]:  # Only show the first 10
            print(
                f"  {proc.pid:<10} {proc.process_name:<30} {proc.gpu_usage:>9.1f}%",
                file=stream,
            )


def render_snapshot(
    gpus: List[GPUInfo],
    snapshot_time: datetime,
    verbose: bool = False,
    file: Optional[TextIO] = None,
) -> None:
    """Render one complete monitoring snapshot."""
    stream = file if file is not None else sys.stdout
    print(f"\n{'=' * 70}", file=stream)
    print(
        f"GPU-SMI (Windows) - {snapshot_time.strftime('%Y-%m-%d %H:%M:%S')}",
        file=stream,
    )
    print(f"{'=' * 70}", file=stream)

    for gpu in gpus:
        print_gpu_info(gpu, verbose=verbose, file=stream)


def main():
    # Set console output encoding to UTF-8 on Windows without buffering loop output
    configure_stdout()

    parser = argparse.ArgumentParser(
        description="Simple Windows GPU monitoring tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python gpu-smi.py              # Show all GPUs
  python gpu-smi.py -i 0         # Show a specific GPU
  python gpu-smi.py -l 5         # Refresh every 5 seconds
  python gpu-smi.py -v           # Show detailed information (including processes)
        """,
    )
    parser.add_argument(
        "-i",
        "--gpu",
        type=int,
        default=None,
        help="GPU index to display (default: all)",
    )
    parser.add_argument(
        "-l",
        "--loop",
        type=parse_refresh_interval,
        metavar="SECONDS",
        help="Refresh interval in seconds",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed information"
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.0")

    args = parser.parse_args()

    smi = WindowsGPUMonitor()

    if not smi._gpu_adapters:
        print("Error: no GPU devices found", file=sys.stderr)
        sys.exit(1)

    try:
        while True:
            iteration_started_at = time.monotonic()

            if args.gpu is not None:
                gpu = smi.get_gpu_info(args.gpu)
                if gpu:
                    gpus = [gpu]
                else:
                    print(f"Error: GPU {args.gpu} does not exist", file=sys.stderr)
                    sys.exit(1)
            else:
                gpus = smi.get_all_gpus()

            snapshot_time = datetime.now()
            buffer = io.StringIO()
            render_snapshot(gpus, snapshot_time, verbose=args.verbose, file=buffer)

            if args.loop is not None and sys.stdout.isatty():
                os.system("cls" if os.name == "nt" else "clear")

            sys.stdout.write(buffer.getvalue())

            sys.stdout.flush()

            if args.loop is not None:
                remaining_sleep = max(
                    args.loop - (time.monotonic() - iteration_started_at), 0.0
                )
                if remaining_sleep > 0:
                    time.sleep(remaining_sleep)
            else:
                break

    except KeyboardInterrupt:
        print("\n\nExiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
