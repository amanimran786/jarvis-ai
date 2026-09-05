# Build Your Own Local AI Agents on a Mac

This guide reproduces the Jarvis V2 foundation from a clean Apple Silicon Mac.
It creates a local MLX-LM model server and a bounded agent that can inspect a
code workspace without a cloud model or API key.

The design follows Apple's four-layer pattern: MLX, MLX-LM, an
OpenAI-compatible localhost server, and an agent client. Apple demonstrates the
same stack, structured tool calling, continuous batching, and optional
multi-Mac inference in [Run local agentic AI on the Mac using MLX](https://developer.apple.com/videos/play/wwdc2026/232/).

## What “fully local” means

After installation:

- prompts and model responses stay on the Mac
- model inference runs on Apple Silicon through MLX
- the server listens only on `127.0.0.1`
- Jarvis V2 accepts no API key and sends no authorization header
- the installed service loads weights in offline mode
- files and tool results are handled by the local agent process

A fresh machine normally needs internet access once to clone the repository,
install Python packages, and download open model weights. After those artifacts
exist locally, runtime operation does not require internet. An air-gapped setup
can transfer the repository, Python wheelhouse, and model snapshot from another
machine instead.

## 1. Check the Mac

Requirements:

- Apple Silicon Mac (M1 or newer)
- macOS with current security updates
- at least 20 GB free disk space for the starter environment and model
- Python 3.12
- Git and Xcode Command Line Tools

Check the hardware and tools:

```bash
uname -m
sysctl -n machdep.cpu.brand_string
sysctl -n hw.memsize
python3.12 --version
git --version
```

`uname -m` should report `arm64`. If the developer tools are missing:

```bash
xcode-select --install
```

Conservative model starting points:

| Unified memory | Start with | Guidance |
|---|---|---|
| 8–16 GB | 3B–4B, 4-bit | Keep concurrency and context small |
| 24–32 GB | 7B–8B, 4-bit | Good setup and tool-loop tier |
| 36–48 GB | 8B–14B, 4-bit | Benchmark larger MoE models before adopting them |
| 64 GB+ | 14B–30B-class quantized | Measure memory at the intended concurrency |

These are starting points, not guarantees. Context length, KV cache,
concurrency, and model architecture all consume additional unified memory.

## 2. Clone the V2 branch

```bash
git clone --branch codex/v2 \
  https://github.com/amanimran786/jarvis-ai.git
cd jarvis-ai
```

The frozen V1 recovery point is tag `jarvis-v1-final-2026-09-04`. Do not start
the V1 launch agents when building a new V2 machine.

## 3. Create an isolated Python environment

```bash
python3.12 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements-v2.txt
```

Confirm the server command is present:

```bash
./venv/bin/mlx_lm.server --help
```

The upstream MLX-LM project documents both `pip install mlx-lm` and the server
command in its [official repository](https://github.com/ml-explore/mlx-lm) and
[server reference](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md).

## 4. Download model weights once

Jarvis V2 currently uses this tool-calling bootstrap model:

```bash
./venv/bin/hf download mlx-community/Qwen3-8B-4bit
```

This is the only model-download step. Review the model card and license before
using a different model. Do not assume that “open weights” means there are no
license conditions.

Confirm the snapshot exists:

```bash
find "$HOME/.cache/huggingface/hub/models--mlx-community--Qwen3-8B-4bit/snapshots" \
  -mindepth 1 -maxdepth 1 -type d
```

To prepare an air-gapped Mac, copy the entire model directory shown above to
the same Hugging Face cache location on the destination machine. Build a Python
wheelhouse on a connected Apple Silicon Mac and transfer that too:

```bash
./venv/bin/python -m pip download -r requirements-v2.txt -d wheelhouse
```

On the offline Mac:

```bash
./venv/bin/python -m pip install --no-index \
  --find-links wheelhouse -r requirements-v2.txt
```

## 5. Test the model manually

Start a foreground server first:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
./venv/bin/mlx_lm.server \
  --model mlx-community/Qwen3-8B-4bit \
  --host 127.0.0.1 \
  --port 8080 \
  --decode-concurrency 4 \
  --prompt-concurrency 2 \
  --prompt-cache-size 8 \
  --temp 0.0
```

In a second terminal:

```bash
curl -fsS http://127.0.0.1:8080/v1/models
curl -fsS http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mlx-community/Qwen3-8B-4bit","messages":[{"role":"user","content":"Reply with LOCAL OK"}],"max_tokens":32}'
```

Stop the foreground server with Control-C after both calls succeed.

## 6. Install the persistent offline service

```bash
./venv/bin/python scripts/install_v2_local.py
```

For an existing Jarvis V1 machine, use the explicit transition command:

```bash
./venv/bin/python scripts/install_v2_local.py --remove-v1
```

The latter permanently removes the installed V1 app and legacy LaunchAgent
files. It does not delete the repository or the V1 Git recovery tag.

Verify the service:

```bash
launchctl print "gui/$(id -u)/com.jarvis.v2.model"
lsof -nP -iTCP:8080 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8080/v1/models
```

Expected network binding: `127.0.0.1:8080`. If it says `0.0.0.0:8080`, stop
and fix the configuration before using the server.

Logs are local:

```text
~/Library/Logs/JarvisV2/model-server.log
~/Library/Logs/JarvisV2/model-server.error.log
```

## 7. Run the Jarvis V2 agent

From the repository:

```bash
./venv/bin/python -m jarvis_v2 \
  "Inspect git status and explain what needs attention" \
  --workspace "$PWD"
```

The bootstrap agent can read files inside the selected workspace and run
read-only Git actions. It cannot write files, commit, run arbitrary shell
commands, or escape the workspace. Each run produces:

- an atomic checkpoint: `.jarvis-v2/runs/RUN_ID.json`
- an append-only event log: `.jarvis-v2/runs/RUN_ID.events.jsonl`

Resume a blocked run:

```bash
./venv/bin/python -m jarvis_v2 --resume RUN_ID --workspace "$PWD"
```

## 8. Connect another local agent client

Any client that supports an OpenAI-compatible custom endpoint can use:

```text
Base URL: http://127.0.0.1:8080/v1
Port: 8080
API key: none
Model: mlx-community/Qwen3-8B-4bit
```

In Xcode, open its Intelligence settings, add a locally hosted model provider,
set port `8080`, and label it `MLX`. Interface wording may differ by Xcode
release. The endpoint should remain loopback-only.

## 9. Understand multi-agent concurrency

The persistent server keeps one model resident in unified memory. Multiple
agent processes can submit requests to it, and MLX-LM can continuously batch
compatible requests. The installed V2 profile currently allows four decode
slots and two prompt-prefill slots.

More concurrency is not automatically faster. Measure the exact model and task
on the target Mac. Track total latency, time to first token where streaming is
available, throughput, peak memory, task success, and malformed tool calls.

Run the included heterogeneous three-agent research team:

```bash
./venv/bin/python scripts/run_v2_research_team.py --workspace "$PWD"
```

Run the strict 1/2/4-worker benchmark:

```bash
./venv/bin/python scripts/benchmark_v2_concurrency.py --workspace "$PWD"
```

The team coordinator runs specialists concurrently, captures content digests
for successful tool calls, checks each result against its assignment contract,
and sends only verified evidence to a separate no-tools synthesizer. Team state
is stored under `.jarvis-v2/team-runs/`; benchmark artifacts are stored under
`.jarvis-v2/benchmarks/`. Both directories are local and Git-ignored.

Distributed inference across multiple Macs is optional and is not part of the
single-Mac bootstrap. Apple documents it separately in
[Explore distributed inference and training with MLX](https://developer.apple.com/videos/play/wwdc2026/233/).

## 10. Watch a run in the local pipeline dashboard

Start the read-only developer dashboard in one terminal:

```bash
./venv/bin/python scripts/v2_dashboard.py --open
```

The command prints and opens a process-specific capability URL on
`127.0.0.1:7878`. The dashboard does not start agents or acquire their run
leases. It observes checkpoints under `.jarvis-v2/` and probes only the validated
loopback model endpoint. Keep the capability URL private and stop the foreground
server with Control-C when finished.

Run an instrumented single agent or the three-worker demo in another terminal:

```bash
./venv/bin/python scripts/v2_trace.py \
  "Inspect git status and report what needs attention"
./venv/bin/python scripts/v2_trace.py --team
```

Trace files default to metadata only: actor IDs, event timing, tool names,
character counts, and SHA-256 evidence. Raw task text, model previews, tool
arguments, results, and exception messages are omitted. For deliberate
foreground debugging they can be included with `--include-sensitive-content`.

Agent checkpoints are different: they contain the full local conversation so a
run can be resumed. They are mode `0600`, and the dashboard fetches those
messages only after the protected **Show conversation** control is selected.
Treat `.jarvis-v2/` as sensitive local work data.

This dashboard is a developer observer, not the future packaged desktop UI. It
must remain foreground-only until the desktop-app phase adds a stronger process
and user-session boundary.

## 11. Security boundary

The upstream MLX-LM server documentation says the server is not recommended as
a production network service because it implements only basic security checks.
Jarvis therefore binds it to loopback and places tool validation in the agent,
not the model server.

Do not:

- bind the server to `0.0.0.0`
- expose port 8080 through a tunnel or router
- treat model output as trusted commands
- give write or shell tools to the model without explicit authorization,
  scoped paths, timeouts, logging, and result verification
- claim a task succeeded without checking its saved tool evidence

Local ownership removes hosted-provider control over inference. It does not
make model output correct or safe by default.

## 12. Test and diagnose

Run the focused V2 tests:

```bash
./venv/bin/python -m pip install -r requirements-v2-dev.txt
./venv/bin/python -m pytest \
  tests/test_jarvis_v2_local_runtime.py \
  tests/test_jarvis_v2_team.py \
  tests/test_v2_observability.py \
  tests/test_install_v2_local.py -q
```

Common problems:

| Symptom | Check | Fix |
|---|---|---|
| `mlx_lm.server` missing | `./venv/bin/python -m pip show mlx-lm` | Reinstall `requirements-v2.txt` in the same venv |
| Installer refuses model download | Check the Hugging Face cache path | Complete step 4 while connected, then rerun |
| Port 8080 busy | `lsof -nP -iTCP:8080` | Stop the conflicting local process or deliberately select another loopback port |
| Service repeatedly exits | Read `model-server.error.log` | Check model integrity, free memory, and executable paths |
| Metal out-of-memory abort | Run `ollama ps`, `memory_pressure -Q`, then inspect `model-server.error.log` | Let other large resident models unload or stop them deliberately before retrying; one 48 GB Mac cannot be assumed to hold every local model and cache at once |
| Agent rejects endpoint | Inspect `--endpoint` | Use explicit `http://127.0.0.1:PORT/v1`; remote and hostname URLs are intentionally rejected |
| Mac becomes memory-constrained | Reduce model/context/concurrency | Start with a smaller 4-bit model and benchmark again |

The model listing endpoint can remain healthy immediately after launchd
restarts a crashed service. A successful `/v1/models` probe proves identity and
reachability, not that enough unified memory exists for the next generation.
For readiness testing, run at least one real completion after checking for
other resident Ollama or MLX models.

## 13. Stop or uninstall V2

Stop the service without deleting weights:

```bash
launchctl bootout "gui/$(id -u)/com.jarvis.v2.model"
```

Disable automatic restart:

```bash
launchctl disable "gui/$(id -u)/com.jarvis.v2.model"
```

Then remove this exact file if a full uninstall is intended:

```text
~/Library/LaunchAgents/com.jarvis.v2.model.plist
```

Model weights remain in the Hugging Face cache until the owner explicitly
removes them.

## Reproduction checklist

- [ ] Apple Silicon and unified memory recorded
- [ ] Python environment isolated
- [ ] MLX-LM version recorded
- [ ] Model name, quantization, and license reviewed
- [ ] Model snapshot cached locally
- [ ] Offline environment flags enabled
- [ ] Listener verified as `127.0.0.1`, never `0.0.0.0`
- [ ] Model-only curl smoke test passed
- [ ] V2 agent tool-loop smoke test passed
- [ ] 1/2/4-worker verified concurrency benchmark passed
- [ ] Checkpoint and event log created
- [ ] Focused tests passed
- [ ] Failures and limitations added to the build journal

## V2 visual identity

The repository includes the original V2 guardian artwork and a complete macOS
icon set:

```text
assets/v2/icon_1024.png
assets/v2/icon.iconset/
assets/v2/jarvis-v2.icns
```

The `.icns` file is reserved for the future packaged V2 app. Do not recreate a
Desktop symlink until that real app bundle passes its packaging gate.
