"""
vram_profiler.py
~~~~~~~~~~~~~~~~
Intel Arc GPU의 VRAM 사용량과 GPU Load율을 반환하는 경량 프로파일러.

의존 패키지 없음 — 표준 라이브러리(subprocess, json, os, glob, re)만 사용.

데이터 수집 우선순위:
  1. intel_gpu_top -J  (가장 풍부한 정보, intel-gpu-tools 패키지 필요)
  2. sysfs              (xe / i915 드라이버가 노출하는 경로 탐색)
  3. 두 경로 모두 실패 시 error 키에 사유를 담아 반환

반환 형태:
  {
      "gpu_load_percent": float | None,   # 렌더/3D 엔진 사용률 0~100 (%)
      "vram_used_mb":     float | None,   # VRAM 사용량 (MiB)
      "vram_total_mb":    float | None,   # VRAM 전체 용량 (MiB)
      "source":           str,            # "intel_gpu_top" | "sysfs" | "intel_gpu_top+sysfs" | "unavailable"
      "error":            str | None,     # 실패 시 원인 메시지, 성공 시 None
  }
"""

import glob
import json
import os
import re
import subprocess
from typing import Optional


# ---------------------------------------------------------------------------
# intel_gpu_top 경로
# ---------------------------------------------------------------------------

def _run_intel_gpu_top() -> Optional[dict]:
    """
    `intel_gpu_top -J -s 200` 를 실행해 JSON 한 샘플을 파싱해 반환.
    명령이 없거나 실패하면 None 반환.
    """
    try:
        proc = subprocess.Popen(
            ["intel_gpu_top", "-J", "-s", "200"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()

        if not stdout.strip():
            return None

        # --- 파싱 전략 1: 줄 단위 NDJSON ---
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass

        # --- 파싱 전략 2: 붙어있는 JSON 오브젝트 순차 디코딩 ---
        decoder = json.JSONDecoder()
        idx, last_obj = 0, None
        while idx < len(stdout):
            try:
                obj, end = decoder.raw_decode(stdout, idx)
                last_obj = obj
                idx = end
                while idx < len(stdout) and stdout[idx] in " \t\n\r":
                    idx += 1
            except json.JSONDecodeError:
                idx += 1
        return last_obj

    except FileNotFoundError:
        return None
    except Exception:
        return None


def _parse_gpu_load(data: dict) -> Optional[float]:
    """engines 딕셔너리에서 Render/3D 엔진의 busy % 추출."""
    engines = data.get("engines", {})
    if not engines:
        return None

    # 우선순위: 렌더/3D 계열 키 탐색
    for key in ("Render/3D/0", "Render/3D", "rcs0", "RCS0", "render"):
        if key in engines:
            val = engines[key].get("busy")
            if val is not None:
                return float(val)

    # fallback: 모든 엔진의 busy 평균
    busy_values = [
        v.get("busy", 0.0)
        for v in engines.values()
        if isinstance(v, dict) and "busy" in v
    ]
    return sum(busy_values) / len(busy_values) if busy_values else None


def _parse_vram_from_json(data: dict) -> tuple[Optional[float], Optional[float]]:
    """
    intel_gpu_top JSON 데이터에서 VRAM 사용량·전체 용량(MiB)을 추출.
    드라이버(xe/i915) 및 버전마다 키 구조가 다르므로 여러 형태를 시도.
    """
    memory = data.get("memory", {})

    # --- xe 드라이버: memory.local 또는 memory.vram ---
    for region_key in ("local", "vram", "VRAM", "Local"):
        region = memory.get(region_key)
        if not isinstance(region, dict):
            continue

        used = region.get("used") if region.get("used") is not None else region.get("resident")
        total = region.get("total") if region.get("total") is not None else region.get("size")
        if used is None:
            continue

        unit = str(region.get("unit", "MiB")).strip()
        multiplier = _unit_to_mib(unit)
        return used * multiplier, (total * multiplier if total is not None else None)

    # --- i915 / 구형: memory.gtt (GGTT ≈ 공유 VRAM 대용) ---
    gtt = memory.get("gtt", {})
    if isinstance(gtt, dict) and gtt.get("used") is not None:
        unit = str(gtt.get("unit", "MiB")).strip()
        multiplier = _unit_to_mib(unit)
        used = gtt["used"] * multiplier
        total = (gtt["total"] * multiplier) if gtt.get("total") is not None else None
        return used, total

    # --- clients RSS 합산 (마지막 수단) ---
    clients = data.get("clients", {})
    if isinstance(clients, dict):
        total_rss = sum(
            c.get("memory", {}).get("rss", 0)
            for c in clients.values()
            if isinstance(c, dict)
        )
        if total_rss:
            return total_rss / (1024 * 1024), None

    return None, None


def _unit_to_mib(unit: str) -> float:
    """단위 문자열 → MiB 변환 계수."""
    u = unit.lower()
    if u in ("b", "bytes"):
        return 1 / (1024 * 1024)
    if u in ("kib", "kb"):
        return 1 / 1024
    if u in ("mib", "mb"):
        return 1.0
    if u in ("gib", "gb"):
        return 1024.0
    return 1.0  # 모르면 MiB로 가정


# ---------------------------------------------------------------------------
# sysfs 경로
# ---------------------------------------------------------------------------

_SYSFS_USED_PATTERNS = [
    # Intel Arc / xe 드라이버 (커널 6.2+)
    "/sys/class/drm/card*/device/tile*/gt*/mem_info_vram_used",
    "/sys/class/drm/card*/device/tile*/gt*/vram/mem_used_bytes",
    "/sys/bus/pci/devices/*/tile*/gt*/mem_info_vram_used",
    # i915 드라이버 (일부 버전)
    "/sys/class/drm/card*/device/drm/card*/gt/gt0/mem_info_vram_used",
]

_SYSFS_TOTAL_SUFFIXES = (
    ("mem_info_vram_used", "mem_info_vram_total"),
    ("vram/mem_used_bytes", "vram/mem_total_bytes"),
)


def _read_sysfs_vram() -> tuple[Optional[float], Optional[float]]:
    """sysfs에서 VRAM used / total 바이트를 읽어 MiB 단위로 반환."""
    for pattern in _SYSFS_USED_PATTERNS:
        paths = glob.glob(pattern)
        if not paths:
            continue

        try:
            with open(paths[0]) as f:
                used_bytes = int(f.read().strip())
        except (OSError, ValueError):
            continue

        total_bytes: Optional[int] = None
        for old_suffix, new_suffix in _SYSFS_TOTAL_SUFFIXES:
            total_path = paths[0].replace(old_suffix, new_suffix)
            if os.path.exists(total_path):
                try:
                    with open(total_path) as f:
                        total_bytes = int(f.read().strip())
                    break
                except (OSError, ValueError):
                    pass

        return used_bytes / (1024 * 1024), (
            total_bytes / (1024 * 1024) if total_bytes is not None else None
        )

    # debugfs (root 권한 필요, 없으면 조용히 건너뜀)
    for path in glob.glob("/sys/kernel/debug/dri/*/xe_memory_stats"):
        try:
            with open(path) as f:
                content = f.read()
            m = re.search(
                r"vram\s*[:\-]\s*(\d+)\s*(bytes|KiB|MiB|GiB|KB|MB|GB)",
                content,
                re.IGNORECASE,
            )
            if m:
                val, unit = int(m.group(1)), m.group(2)
                return val * _unit_to_mib(unit), None
        except OSError:
            pass

    return None, None


# ---------------------------------------------------------------------------
# PowerShell (WSL2 전용)
# ---------------------------------------------------------------------------

def _run_powershell_gpu() -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    WSL2 환경에서 powershell.exe를 통해 Windows GPU 정보를 읽는다.
    반환: (gpu_load_percent, vram_used_mb, vram_total_mb)
    - vram_used_mb: WMI로는 불가, 항상 None
    - vram_total_mb: AdapterRAM(bytes) → MiB 변환
    - gpu_load_percent: 3D 엔진 Performance Counter 합산
    """
    try:
        script = r"""
$load = $null
Try {
    $samples = (Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage' -ErrorAction Stop).CounterSamples
    $load = [Math]::Min([Math]::Round(($samples | Measure-Object CookedValue -Sum).Sum, 1), 100)
} Catch {}

$vram = $null
Try {
    $gpu = Get-WmiObject Win32_VideoController | Where-Object { $_.Name -like '*Intel*' } | Select-Object -First 1
    if ($gpu.AdapterRAM) { $vram = [Math]::Round($gpu.AdapterRAM / 1MB, 0) }
} Catch {}

"$load|$vram"
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=6,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, None, None

        parts = result.stdout.strip().split("|")
        gpu_load = float(parts[0]) if parts[0] not in ("", "$null") else None
        vram_total = float(parts[1]) if len(parts) > 1 and parts[1] not in ("", "$null") else None
        return gpu_load, None, vram_total

    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None, None, None


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def get_intel_gpu_stats() -> dict:
    """
    Intel GPU의 현재 VRAM 사용량과 GPU Load율을 반환한다.

    Returns
    -------
    dict
        gpu_load_percent : float | None
            Render/3D 엔진 사용률 (0 ~ 100 %). None이면 측정 불가.
        vram_used_mb : float | None
            현재 VRAM 사용량 (MiB). None이면 측정 불가.
        vram_total_mb : float | None
            VRAM 전체 용량 (MiB). None이면 정보 없음.
        source : str
            데이터 출처. "intel_gpu_top", "sysfs", "intel_gpu_top+sysfs",
            또는 "unavailable".
        error : str | None
            모든 방법이 실패한 경우 원인 메시지. 성공 시 None.

    Notes
    -----
    - `intel_gpu_top` 사용 시 `intel-gpu-tools` 패키지가 설치돼 있어야 한다.
      Ubuntu/Debian: ``sudo apt install intel-gpu-tools``
    - WSL 환경에서는 GPU 접근 권한이 제한될 수 있다.
    """
    result: dict = {
        "gpu_load_percent": None,
        "vram_used_mb": None,
        "vram_total_mb": None,
        "source": "unavailable",
        "error": None,
    }

    # 1) intel_gpu_top
    gpu_data = _run_intel_gpu_top()
    if gpu_data is not None:
        result["gpu_load_percent"] = _parse_gpu_load(gpu_data)
        result["vram_used_mb"], result["vram_total_mb"] = _parse_vram_from_json(gpu_data)
        result["source"] = "intel_gpu_top"

    # 2) VRAM 여전히 미확인 → sysfs 보완
    if result["vram_used_mb"] is None:
        sysfs_used, sysfs_total = _read_sysfs_vram()
        if sysfs_used is not None:
            result["vram_used_mb"] = sysfs_used
            result["vram_total_mb"] = sysfs_total
            result["source"] = (
                "intel_gpu_top+sysfs" if result["source"] == "intel_gpu_top" else "sysfs"
            )

    # 3) 둘 다 실패 → WSL2 PowerShell fallback
    if result["gpu_load_percent"] is None and result["vram_used_mb"] is None:
        ps_load, _, ps_total = _run_powershell_gpu()
        if ps_load is not None or ps_total is not None:
            result["gpu_load_percent"] = ps_load
            result["vram_total_mb"] = ps_total
            result["source"] = "powershell_wmi"

    # 4) 값 반올림
    for key in ("gpu_load_percent", "vram_used_mb", "vram_total_mb"):
        if result[key] is not None:
            result[key] = round(result[key], 2)

    # 5) 완전 실패 시 오류 메시지
    if result["gpu_load_percent"] is None and result["vram_used_mb"] is None:
        result["error"] = (
            "GPU 정보를 가져올 수 없습니다. "
            "Linux 환경: sudo apt install intel-gpu-tools 후 /dev/dri 권한 확인. "
            "WSL2: powershell.exe 접근 가능 여부 확인."
        )

    return result


# ---------------------------------------------------------------------------
# 직접 실행 시 빠른 확인
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint
    pprint.pprint(get_intel_gpu_stats())
