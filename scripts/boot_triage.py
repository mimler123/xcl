"""Capture reproducible XCL startup or graphics diagnostics in a local directory."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


METRICS = (
    "presents", "frame_boundaries", "translated_pipelines", "failed_pipelines",
    "fallback_pipelines", "fallback_root_signatures", "background_pipelines",
)
VALIDATION = re.compile(r"D3D12 debug layer \((error|warning), id (\d+)\): (.*)")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture(command, env, stdout, stderr, timeout):
    started = time.monotonic()
    timed_out = False
    with stdout.open("wb") as output, stderr.open("wb") as errors:
        with subprocess.Popen(command, stdout=output, stderr=errors, env=env) as child:
            try:
                child.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                try:
                    stopped = subprocess.run(
                        [str(taskkill), "/pid", str(child.pid), "/t", "/f"],
                        stdout=errors, stderr=errors, timeout=10,
                    )
                    if stopped.returncode and child.poll() is None:
                        errors.write(b"taskkill could not confirm termination of the XCL host tree\n")
                except (OSError, subprocess.TimeoutExpired):
                    errors.write(b"taskkill could not confirm termination of the XCL host tree\n")
                finally:
                    if child.poll() is None:
                        child.kill()
                if isinstance(error, KeyboardInterrupt):
                    raise
                timed_out = True
    return {
        "exit_code": child.returncode,
        "exit_hex": f"{child.returncode & 0xffffffff:#010x}",
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def summarize(folder):
    summary = {"inspection": None, "runtime": None}
    inspection = folder / "inspect.json"
    if inspection.is_file():
        data = json.loads(inspection.read_text(encoding="utf-8-sig"))
        imports = data.get("imports", [])
        summary["inspection"] = {
            "image": Path(data["file"]).name,
            "entry_rva": data.get("entry_rva"),
            "imports_by_source": dict(sorted(Counter(
                item["status"].partition(" (")[0] for item in imports
            ).items())),
            "stubbed_imports": [
                {key: entry[key] for key in ("dll", "function", "delay")}
                for entry in imports if entry["status"] == "STUB"
            ],
        }
    reports = sorted(folder.glob("profile/*/logs/report.json"))
    if len(reports) > 1:
        raise ValueError("Expected one runtime report in the capture profile")
    if reports:
        path = reports[0]
        relative_path = path.relative_to(folder).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            summary["runtime"] = {"report": relative_path, "read_error": str(error)}
            return summary
        d3d = data.get("d3d12", {})
        summary["runtime"] = {
            "report": relative_path,
            "crash": data.get("crash"),
            "exit_code": data.get("exit_code"),
            "emulated_clzero": data.get("emulated_clzero"),
            "emulated_mwaitx": data.get("emulated_mwaitx"),
            "host_checks": data.get("environment", []),
            "adapter": d3d.get("adapter"),
            "metrics": {key: d3d[key] for key in METRICS if key in d3d},
            "dxil_cache": d3d.get("dxil_cache"),
            "translation_errors": d3d.get("translation_errors", []),
            "unknown_iids": d3d.get("unknown_iids", []),
            "frame_dumps": [bmp.relative_to(folder).as_posix()
                            for bmp in sorted(path.parent.glob("frame_*.bmp"))],
            "d3d12_validation": summarize_validation(path.with_name("xcl.log")),
            "called_stubs": data.get("called_stubs", [])[:30],
        }
    return summary


def summarize_validation(log):
    if not log.is_file():
        return []
    messages = {}
    with log.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            match = VALIDATION.search(line)
            if match:
                level, identifier, example = match.groups()
                entry = messages.setdefault((level, identifier), {
                    "level": level, "id": int(identifier), "count": 0, "first": example,
                })
                entry["count"] += 1
    return sorted(messages.values(), key=lambda item: (item["level"], -item["count"], item["id"]))[:12]


def triage(args):
    executable = args.exe.resolve(strict=True)
    xcl = args.xcl.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=args.output.resolve()))
    print(f"Capture directory {folder}", flush=True)
    # Keep ordinary Windows variables and explicit, supported host component paths.
    inherited_xcl = {"XCL_D3D12_DIR", "XCL_DXC_DIR"}
    env = {key: val for key, val in os.environ.items()
           if not key.upper().startswith("XCL_") or key.upper() in inherited_xcl}
    env["XCL_DATA_ROOT"] = str(folder / "profile")
    if args.run:
        env["XCL_RUN_SECONDS"] = str(args.seconds)
    if args.adapter:
        env["XCL_ADAPTER"] = args.adapter
    if args.dxc_threads is not None:
        env["XCL_DXC_THREADS"] = str(args.dxc_threads)
    if args.dxil_cache is not None:
        env["XCL_DXIL_CACHE"] = str(args.dxil_cache.resolve())
    if args.d3d12_debug:
        env["XCL_D3D12_DEBUG"] = "1"
    if args.experimental_mesh_stream:
        env["XCL_NORMALIZE_MESH_STREAM"] = "1"
    if args.graphics:
        env.update(XCL_TRACE_FRAME="1", XCL_TRACE_FRAME_EVENTS="1",
                   XCL_FRAME_DUMP="1", XCL_DRED="1")
    if args.first_frames is not None or args.graphics:
        env["XCL_FRAME_DUMP_FIRST"] = str(args.first_frames if args.first_frames is not None else 2)

    metadata = {
        "captured_utc": stamp,
        "xcl_sha256": sha256(xcl),
        "guest_sha256": sha256(executable),
        "options": {key: val for key, val in env.items() if key.startswith("XCL_")},
        "inspection": None,
        "runtime": None,
    }
    inspect_output = folder / "inspect.stdout"
    write_json(folder / "capture.json", metadata)
    try:
        metadata["inspection"] = capture(
            [str(xcl), "inspect", str(executable), "--json"], env,
            inspect_output, folder / "inspect.stderr", 30,
        )
        if metadata["inspection"]["exit_code"] != 0:
            return 1
        inspect_output.rename(folder / "inspect.json")
        if args.run:
            metadata["runtime"] = capture(
                [str(xcl), "run", str(executable)], env,
                folder / "run.stdout.log", folder / "run.stderr.log", args.seconds + 45,
            )
            return int(metadata["runtime"]["timed_out"] or metadata["runtime"]["exit_code"] != 0)
        return 0
    finally:
        write_json(folder / "capture.json", metadata)
        write_json(folder / "summary.json", summarize(folder))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("exe", type=Path, help="Path to the unpacked Xbox executable")
    parser.add_argument("--xcl", type=Path, default=root / "target/release/xcl.exe")
    parser.add_argument("--output", type=Path, required=True, help="Local parent directory for captures")
    parser.add_argument("--run", action="store_true", help="Run the title after static inspection")
    parser.add_argument("--seconds", type=int, default=20, help="XCL runtime budget (default 20)")
    parser.add_argument("--adapter", help="XCL adapter index or name")
    parser.add_argument("--dxc-threads", type=int, help="Number of shader and pipeline workers (1 to 64)")
    parser.add_argument("--dxil-cache", type=Path, help="Use a writable shader cache shared across captures")
    parser.add_argument("--graphics", action="store_true", help="Capture frame traces, thumbnails and DRED")
    parser.add_argument("--first-frames", type=int, help="Number of initial thumbnails (default 2 with --graphics)")
    parser.add_argument("--d3d12-debug", action="store_true", help="Enable Windows D3D12 validation messages")
    parser.add_argument("--experimental-mesh-stream", action="store_true",
                        help="Try the diagnostic console mesh stream retagging path")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("XCL requires Windows")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if (args.graphics or args.d3d12_debug or args.experimental_mesh_stream) and not args.run:
        parser.error("--graphics, --d3d12-debug and --experimental-mesh-stream require --run")
    if args.dxc_threads is not None and not 1 <= args.dxc_threads <= 64:
        parser.error("--dxc-threads must be between 1 and 64")
    if args.first_frames is not None and (args.first_frames < 0 or not args.run):
        parser.error("--first-frames requires --run and a nonnegative value")
    try:
        return triage(args)
    except (OSError, ValueError, KeyError) as error:
        print(f"Boot triage failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
