# Windows boot triage

Build XCL in release mode before starting. The capture script uses Python 3. It accepts an explicit executable from an unpacked title and creates a fresh XCL profile for each run.

```powershell
cargo build --locked --release --workspace
py -3 .\scripts\boot_triage.py D:\Games\Title\game.exe --output D:\Captures\xcl
py -3 .\scripts\boot_triage.py D:\Games\Title\game.exe --output D:\Captures\xcl --run --seconds 20
py -3 .\scripts\boot_triage.py D:\Games\Title\game.exe --output D:\Captures\xcl --run --seconds 30 --graphics
```

Use `--xcl` to compare builds, `--adapter` to select a GPU, and `--dxc-threads` to try fewer shader and pipeline workers after an allocation failure. The script records the input and XCL binary hashes. It starts each run with a cold XCL shader cache. The title's own timeout allows report finalization. If startup or shutdown hangs, the script attempts to terminate the XCL process tree at the outer timeout.

## Debugging a run

1. Start with `capture.json` and `summary.json`. Check the process exit code, timeout flag, host checks, and whether XCL wrote a runtime report. With an early failure, read `run.stderr.log` and the end of `profile/<title>/logs/xcl.log`.
2. Compare `inspect.json` with the runtime report. Static stubs show what may be missing. The first runtime caller and the order of events in `xcl.log` help identify which stubs actually matter. The `emulated_clzero` counter shows how often XCL handled the AMD cache-line instruction on a host that lacks it. [AMD instruction reference](https://docs.amd.com/api/khub/documents/j44LvPXzuuXgM0WyHKQfeQ/content)
3. For graphics failures, rerun with `--graphics`. Compare frame boundaries, presents, and failed or fallback pipelines. The first two shown frames and periodic thumbnails are captured when readback succeeds. Use them to verify visible output. Locate the first relevant translation error or device removal before investigating later failures. For a rejected pipeline, rerun with `--d3d12-debug` to collect Windows validation messages in `xcl.log` and group them by message ID in the summary.
4. Rerun the same command after each change. Use the binary hashes and adapter recorded in the capture to verify what changed. Keep captures, package files, saves, and shader dumps local.

`--experimental-mesh-stream` enables a diagnostic path for console streams that tag mesh bytecode as an amplification shader. It is off by default. The first validation run through this path hit an access violation inside the Windows D3D12 debug layer during mesh pipeline creation.

## Using a coding assistant

Point the assistant at the local `capture.json`, `summary.json`, and `xcl.log`. Give it the previous successful capture when one exists. Ask it to quote the first reproducible failure, identify the responsible XCL code path, and run the smallest relevant test. For a suspected shader issue, use the existing XCL shader tools on the affected record before scanning a complete archive.
