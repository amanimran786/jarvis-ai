# Guarded Local Model Improvement

Jarvis improves local-model candidates through an explicit, auditable pipeline:

`capture -> sanitize -> curate -> split -> teach -> train -> evaluate -> human approval -> canary -> promote or rollback`

The pipeline is local-only. Ordinary conversations are not trusted training
data, cloud teachers are disabled, the held-out test split is immutable, and no
candidate is promoted without an exact digest match plus two human confirmation
challenges. Artifacts live under `JARVIS_DATA_DIR/training/improvement_loop` and
are never stored in Git.

## Model roles

- `qwen3:30b-a3b`: reasoning/planning teacher and critic
- `devstral:latest`: coding teacher and code-task critic
- `qwen3:8b`: MLX-LM LoRA/QLoRA student
- `nomic-embed-text:latest`: semantic deduplication
- `jarvis-local:latest`: promoted baseline alias

Every lookup uses Jarvis's shared exact Ollama tag matcher. Missing tags or
digests stop the stage; there is no fallback to another local or cloud model.

## Read-only checks

```bash
./venv/bin/python scripts/local_improvement.py fleet
./venv/bin/python scripts/local_improvement.py dry-run
./venv/bin/python scripts/local_improvement.py status
```

The dry-run reads model inventory and pipeline status but writes no pipeline
files. Status is also available from `GET /local/improvement/status`, and the
existing training dashboard shows split counts, quarantine count, candidate,
baseline, approval state, digest, and rollback target.

## Capture and curate

Capture accepts only `thumbs_up`, `thumbs_down`, `corrected_answer`,
`successful_tool_trace`, `failed_eval`, or `approved_teacher`. The JSON payload
must include explicit `user_approved` state, provenance, task type, score, and
the exact source model tag/digest. Use `human` for both model fields on a direct
human correction.

```bash
./venv/bin/python scripts/local_improvement.py capture --payload approved-example.json
./venv/bin/python scripts/local_improvement.py curate
./venv/bin/python scripts/local_improvement.py split --curated \
  "$JARVIS_DATA_DIR/training/improvement_loop/curated/curated_HASH.jsonl"
./venv/bin/python scripts/local_improvement.py teach --dataset-id ds_HASH
```

Secret patterns and sensitive personal data are redacted. Prompt injections,
secret-bearing examples, private-file content, malformed rows, and suspicious
examples are quarantined after redaction.
Semantic duplicates are removed with `nomic-embed-text`; human corrections have
the highest priority. Teacher corrections must pass deterministic validation
and a different local critic. The teacher cannot approve its own answer.

The existing `/feedback` endpoint remains the live feedback source. It writes to
the guarded dataset only when `approve_for_training=true` is supplied together
with a model digest, score, and explicit correction/expected answer; ordinary
feedback remains evaluation evidence only. `learner.py` continues to manage
personal memory, while `self_eval.py` contributes numeric provenance to an
explicitly approved feedback item. Neither module's raw conversation content is
automatically ingested.

## Training and resume

Training is bounded to 2,000 iterations, checks disk and unified memory, uses a
fixed seed, validation checkpoints, gradient checkpointing, and MLX-LM
`--mask-prompt` completion-only loss. It never consumes `test.jsonl`.

```bash
export JARVIS_LOCAL_TRAINING_APPROVED=1
./venv/bin/python scripts/local_improvement.py train \
  --dataset-id ds_HASH \
  --teach-dir "$JARVIS_DATA_DIR/training/improvement_loop/manifests/ds_HASH/teach/HASH" \
  --human-approved --iters 400

# Resume an interrupted run from a saved adapter weights file.
./venv/bin/python scripts/local_improvement.py train \
  --dataset-id ds_HASH --teach-dir /trusted/teach/HASH \
  --human-approved --iters 400 --run-id run_YYYYMMDD_HHMMSS_HASH \
  --resume-adapter-file "$JARVIS_DATA_DIR/training/improvement_loop/runs/run_ID/adapter/adapters.safetensors"
```

The existing MLX training module remains the only trainer. After training, the
pipeline exposes deterministic fuse, GGUF conversion, Modelfile, and versioned
`ollama create jarvis-local:candidate-YYYYMMDD-RUNSUFFIX` commands. GGUF conversion
requires `JARVIS_LLAMA_CPP_CONVERTER` to point to a trusted local
`convert_hf_to_gguf.py`; it fails closed when that tool is not configured.

## Evaluation, canary, promotion, rollback

Evaluation runs the candidate and current baseline on identical chat, coding,
planning, tool-schema, memory, hallucination, injection, privacy, approval, and
latency prompts. Promotion requires at least 80% deterministic pass rate, a
strictly positive score delta, no security/tool regression, acceptable latency,
an unchanged candidate digest, human approval, and a zero-tool shadow canary.

```bash
./venv/bin/python scripts/local_improvement.py evaluate \
  --candidate jarvis-local:candidate-YYYYMMDD-RUNSUFFIX --candidate-digest SHA256

# Copy the exact challenge printed by evaluate.
./venv/bin/python scripts/local_improvement.py approve \
  --eval-id eval_HASH --approver "Aman Imran" --confirmation "APPROVE ..."

./venv/bin/python scripts/local_improvement.py canary \
  --eval-id eval_HASH --prompt "Shadow prompt one" --prompt "Shadow prompt two"

./venv/bin/python scripts/local_improvement.py promotion-plan --eval-id eval_HASH
./venv/bin/python scripts/local_improvement.py promote \
  --eval-id eval_HASH --confirmation "PROMOTE ..."

# The status output provides this exact confirmation and rollback tag.
./venv/bin/python scripts/local_improvement.py rollback \
  --confirmation "ROLLBACK jarvis-local:rollback-TIMESTAMP SHA256"
```

Promotion first preserves the current baseline under a versioned rollback tag,
then updates the stable Ollama alias. It never rewrites Jarvis configuration or
self-modifies source code. The legacy automatic model loop and legacy direct
promotion endpoint now fail closed; promotion is possible only through this
guarded flow.
