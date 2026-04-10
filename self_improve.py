"""
Self-improvement engine for Jarvis.

Jarvis can read its own source code, identify weaknesses, propose improvements,
apply changes, and restart itself. Every change is backed up first.

Triggered by:
  - "improve yourself"
  - "upgrade your routing"
  - "fix your memory system"
  - "make yourself better at coding tasks"
  - Automatically after detecting repeated failures in a conversation
"""

import os
import shutil
import subprocess
import sys
import difflib
import tempfile
import re
from datetime import datetime
from brains.brain_claude import ask_claude
from config import OPUS
import evals

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, ".jarvis_backups")
CRASH_LOG = os.path.join(BASE_DIR, ".jarvis_crash.log")

# Source files Jarvis is allowed to improve
IMPROVABLE_FILES = {
    "router.py":        "intent detection and tool routing logic",
    "model_router.py":  "smart model selection and cost optimization",
    "memory.py":        "memory storage and context retrieval",
    "learner.py":       "self-learning and knowledge extraction",
    "brains/brain.py":         "OpenAI GPT interface",
    "brains/brain_claude.py":  "Anthropic Claude interface",
    "brains/brain_ollama.py":  "local Ollama model interface",
    "brains/brain_gemini.py":  "Gemini interface",
    "tools.py":         "system tools (search, apps, timers, system control)",
    "voice.py":         "speech recognition and text-to-speech",
    "config.py":        "model configuration and system prompt",
    "briefing.py":      "morning briefing and startup logic",
    "notes.py":         "note taking system",
    "terminal.py":      "file system and terminal execution",
    "google_services.py": "Google Calendar and Gmail integration",
    "camera.py":        "webcam and vision capabilities",
    "vault.py":         "local markdown vault indexing and retrieval",
    "wiki_builder.py":  "deterministic vault wiki compiler",
    "source_ingest.py": "source ingestion into the local vault",
    "skill_factory.py": "skill generation and eval promotion",
    "skills.py":        "local skill registry and loading",
    "ui.py":            "graphical interface, chat window, layout, colors, and controls",
    "self_improve.py":  "self-improvement and code editing engine",
    "stealth.py":       "screen share invisibility",
    "main.py":          "startup logic and entry point",
    "local_runtime/local_beta.py": "local beta testing harness",
    "local_runtime/local_model_automation.py": "local model automation loop",
    "local_runtime/local_model_benchmark.py": "local model benchmark runner",
    "local_runtime/local_model_eval.py": "local model evaluation harness",
    "local_runtime/local_stt.py": "local speech-to-text runtime",
    "local_runtime/local_training.py": "local training and distillation tools",
    "local_runtime/local_tts.py": "local text-to-speech runtime",
}


# ── Backup system ─────────────────────────────────────────────────────────────

def _backup(filename: str) -> str:
    """Create a timestamped backup of a file before modifying it."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    src = os.path.join(BASE_DIR, filename)
    dst = os.path.join(BACKUP_DIR, f"{filename}.{ts}.bak")
    shutil.copy2(src, dst)
    return dst


def list_backups(filename: str = None) -> list[str]:
    """List all backups, optionally filtered by filename."""
    if not os.path.exists(BACKUP_DIR):
        return []
    files = os.listdir(BACKUP_DIR)
    if filename:
        files = [f for f in files if f.startswith(filename)]
    return sorted(files, reverse=True)


def restore_backup(backup_name: str) -> str:
    """Restore a file from backup."""
    src = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.exists(src):
        return f"Backup not found: {backup_name}"
    # Extract original filename (everything before the timestamp)
    parts = backup_name.rsplit(".", 3)
    original = parts[0] + "." + parts[1] if len(parts) >= 3 else backup_name
    dst = os.path.join(BASE_DIR, original)
    shutil.copy2(src, dst)
    return f"Restored {original} from {backup_name}."


# ── Diff and apply ────────────────────────────────────────────────────────────

def _diff(original: str, updated: str, filename: str) -> str:
    """Generate a human-readable diff."""
    orig_lines = original.splitlines(keepends=True)
    new_lines = updated.splitlines(keepends=True)
    diff = difflib.unified_diff(orig_lines, new_lines, fromfile=f"{filename} (before)",
                                 tofile=f"{filename} (after)", lineterm="")
    return "".join(diff)


def _validate_python_code(filename: str, code: str) -> None:
    """Reject empty or syntactically invalid model output before touching disk."""
    if not code or not code.strip():
        raise ValueError(f"Generated code for {filename} was empty.")

    try:
        compile(code, f"<generated {filename}>", "exec")
    except SyntaxError as e:
        detail = f"line {e.lineno}: {e.msg}"
        lines = code.splitlines()
        if e.lineno and 0 < e.lineno <= len(lines):
            detail += f" near {lines[e.lineno - 1].strip()[:120]!r}"
        raise ValueError(f"Generated code for {filename} failed syntax validation at {detail}.") from e


def _sanitize_generated_code(code: str) -> str:
    """
    Normalize common model-output artifacts before syntax validation.
    This keeps decorative Unicode separators or smart punctuation from breaking
    otherwise valid generated Python.
    """
    replacements = str.maketrans({
        "─": "-",
        "—": "-",
        "–": "-",
        "“": '"',
        "”": '"',
        "’": "'",
        "‘": "'",
    })
    lines = []
    for raw_line in code.splitlines():
        line = raw_line.translate(replacements)
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and re.match(r"^[-]{2,}\s*[A-Za-z].*$", stripped):
            line = "# " + stripped
        lines.append(line)
    return "\n".join(lines)


def _extract_syntax_error_line(error_text: str) -> int | None:
    match = re.search(r"line\s+(\d+)", error_text or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def _heuristic_comment_fix(code: str, error_text: str, attempts: int = 3) -> str:
    """
    If the model emits plain-English section headings as bare code lines,
    comment those lines and retry locally before giving up.
    """
    lines = code.splitlines()
    for _ in range(attempts):
        try:
            compile("\n".join(lines), "<heuristic_fix>", "exec")
            return "\n".join(lines)
        except SyntaxError as exc:
            lineno = exc.lineno or _extract_syntax_error_line(error_text)
            if not lineno or lineno < 1 or lineno > len(lines):
                break
            candidate = lines[lineno - 1]
            stripped = candidate.strip()
            if not stripped or stripped.startswith("#"):
                break
            if re.match(r"^[A-Z][A-Za-z0-9 ,.:;_()/'\"-]+$", stripped):
                lines[lineno - 1] = "# " + stripped
                continue
            break
    return "\n".join(lines)


def apply_improvement(filename: str, new_code: str) -> tuple[str, str]:
    """
    Apply new code to a file. Creates backup first.
    Returns (backup_path, diff_summary).
    """
    path = os.path.join(BASE_DIR, filename)
    with open(path, encoding="utf-8") as f:
        original = f.read()

    _validate_python_code(filename, new_code)
    backup_path = _backup(filename)
    diff = _diff(original, new_code, filename)
    fd, temp_path = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=BASE_DIR, text=True)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_code)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    return backup_path, diff


# ── Self-analysis ─────────────────────────────────────────────────────────────

def read_source(filename: str) -> str:
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return f"File not found: {filename}"
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_all_source() -> str:
    """Read all improvable source files into a single context string."""
    parts = []
    for filename, description in IMPROVABLE_FILES.items():
        content = read_source(filename)
        parts.append(f"### {filename} ({description})\n```python\n{content}\n```")
    return "\n\n".join(parts)


def analyze_weakness(area: str = None) -> str:
    """
    Ask Opus to analyze Jarvis's source code and identify the most impactful improvements.
    area: optional specific area to focus on (e.g. "routing", "memory", "voice")
    """
    focus = f"Focus specifically on: {area}." if area else "Identify the most impactful improvement across any file."

    # Read relevant files
    if area:
        relevant = {k: v for k, v in IMPROVABLE_FILES.items() if area.lower() in v.lower() or area.lower() in k.lower()}
        if not relevant:
            relevant = IMPROVABLE_FILES
    else:
        relevant = IMPROVABLE_FILES

    source_parts = []
    for filename in list(relevant.keys())[:5]:  # limit context
        content = read_source(filename)
        source_parts.append(f"### {filename}\n```python\n{content[:3000]}\n```")
    source = "\n\n".join(source_parts)

    prompt = f"""You are analyzing the source code of Jarvis, a personal AI assistant.
{focus}

Source code:
{source}

Identify the single most impactful improvement that would make Jarvis more capable,
efficient, or useful. Be specific about:
1. Which file to change
2. What the problem is
3. What the improvement is
4. Why it matters

Keep your response concise — 3-4 sentences max."""

    return ask_claude(prompt, model=OPUS)


def _recent_crash_lines(limit: int = 5) -> list[str]:
    if not os.path.exists(CRASH_LOG):
        return []
    try:
        with open(CRASH_LOG, encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
    except OSError:
        return []

    timestamps = [line for line in lines if line.startswith("[")]
    return timestamps[-limit:]


def self_review(area: str | None = None) -> dict:
    """
    Build an evidence-backed self-review report.
    This does not modify code. It summarizes current shortcomings, likely target
    files, and whether there is enough recent evidence to justify self-improve.
    """
    summary = evals.summary()
    failures = summary.get("recent_failures", [])
    if area:
        area_lower = area.lower()
        filtered = [
            failure for failure in failures
            if area_lower in " ".join(
                str(failure.get(key, "")) for key in ("category", "issue", "expected", "user_input", "response")
            ).lower()
        ]
        if filtered:
            failures = filtered

    category_counts = {}
    for failure in failures:
        category = failure.get("category", "general_quality")
        category_counts[category] = category_counts.get(category, 0) + 1

    ordered_categories = sorted(category_counts.items(), key=lambda item: item[1], reverse=True)
    target_file = evals.build_improvement_brief(area=area, min_failures=2).get("target_file") if failures else None
    if not target_file and failures:
        target_file = evals.build_improvement_brief(area=area, min_failures=1).get("target_file")

    shortcomings = []
    next_steps_map = {
        "stability": "stabilize the runtime and inspect recent crash evidence before changing deeper behavior",
        "self_improve": "keep self-improve evidence-gated and policy-aware before allowing edits",
        "memory": "anchor more short answers in stored context and projects instead of generic filler",
        "knowledge": "tighten vault retrieval, citations, and summarization so knowledge answers stay concise and grounded",
        "browser": "improve browser action recovery and page-state verification",
        "routing": "tighten intent classification so explanation requests do not trigger actions",
        "tool_execution": "make tool failures explicit and keep recovery paths deterministic",
        "formatting": "strip spoken-output artifacts before TTS",
        "hallucination": "ground answers in runtime state or local evidence before asserting facts",
    }
    for category, count in ordered_categories[:4]:
        recent = next((f for f in reversed(failures) if f.get("category") == category), None)
        issue = recent.get("issue", "") if recent else ""
        shortcomings.append(
            {
                "category": category,
                "count": count,
                "issue": issue,
                "next_step": next_steps_map.get(category, "improve the repeated failing path before broader changes"),
            }
        )

    evidence_bundle = evals.build_improvement_brief(area=area, min_failures=2)
    ready_for_improvement = bool(evidence_bundle.get("ok"))
    crash_lines = _recent_crash_lines()

    review = {
        "ok": True,
        "area": area or "",
        "shortcomings": shortcomings,
        "ready_for_improvement": ready_for_improvement,
        "target_file": evidence_bundle.get("target_file") if ready_for_improvement else target_file,
        "recommended_instruction": evidence_bundle.get("instruction") if ready_for_improvement else "",
        "failure_ids": evidence_bundle.get("failure_ids", []) if ready_for_improvement else [f.get("id") for f in failures[-5:]],
        "recent_crashes": crash_lines,
        "recent_failure_count": len(failures),
        "category_counts": dict(category_counts),
    }
    return review


def review_text(review: dict) -> str:
    if not review.get("ok"):
        return review.get("error", "Self-review failed.")

    shortcomings = review.get("shortcomings", [])
    if not shortcomings:
        return (
            "I do not have enough recent evidence to name a concrete shortcoming with confidence yet. "
            "The right move is to keep logging failures and only self-edit when there is a repeated pattern."
        )

    top = shortcomings[0]
    parts = [
        f"My top current shortcoming is {top['category']}, based on {top['count']} recent failure signals."
    ]
    if top.get("issue"):
        parts.append(f"The clearest example is: {top['issue']}")
    if top.get("next_step"):
        parts.append(f"The next fix should be to {top['next_step']}.")
    if review.get("target_file"):
        parts.append(f"The most likely target file is {review['target_file']}.")
    if review.get("ready_for_improvement"):
        parts.append("I have enough recent evidence to justify a self-improvement pass on that path.")
    else:
        parts.append("I do not yet have enough repeated evidence to justify editing code automatically.")
    if review.get("recent_crashes"):
        parts.append(f"I also have recent crash evidence in the local crash log, which means runtime stability is part of the review context.")
    return " ".join(parts)


def generate_improvement(filename: str, instruction: str, evidence_bundle: dict | None = None) -> str:
    """
    Ask Opus to rewrite a specific file with an improvement applied.
    Returns the new code as a string.
    """
    current_code = read_source(filename)
    all_context = ""

    # Include related files for context
    for f in list(IMPROVABLE_FILES.keys())[:4]:
        if f != filename:
            content = read_source(f)
            all_context += f"\n\n### {f} (for context)\n```python\n{content[:1500]}\n```"

    evidence_text = ""
    if evidence_bundle and evidence_bundle.get("ok"):
        evidence_lines = "\n".join(f"- {line}" for line in evidence_bundle.get("evidence_lines", []))
        evidence_text = f"\n\nRecent failure evidence:\n{evidence_lines}\n"

    prompt = f"""You are improving Jarvis, a personal AI assistant.

Task: {instruction}

Current {filename}:
```python
{current_code}
```

Related files for context:{all_context}{evidence_text}

Rewrite {filename} with the improvement applied.
Rules:
- Keep all existing functionality intact unless explicitly told to remove it
- Make the improvement clean and well-integrated
- Do not add unnecessary complexity
- The change must directly address the attached recent failure evidence when evidence is provided
- Return ONLY the complete new Python code for {filename}, nothing else
- No explanation, no markdown, just the raw Python code"""

    new_code = ask_claude(prompt, model=OPUS)

    # Strip markdown code fences — handle leading blank lines and language tags
    new_code = new_code.strip()
    if new_code.startswith("```"):
        lines = new_code.split("\n")
        # Drop opening fence (e.g. ```python)
        lines = lines[1:]
        # Drop closing fence if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        new_code = "\n".join(lines)

    return _sanitize_generated_code(new_code)


def repair_generated_code(filename: str, broken_code: str, validation_error: str) -> str:
    prompt = f"""You are repairing generated Python for Jarvis.

The file is {filename}.
The generated code failed syntax validation with this error:
{validation_error}

Repair the syntax while preserving the intended behavior.
Rules:
- Return ONLY complete valid Python code for {filename}
- Do not explain anything
- Do not include markdown fences
- Keep decorative section labels as Python comments if you keep them at all

Broken code:
```python
{broken_code}
```"""

    fixed = ask_claude(prompt, model=OPUS).strip()
    if fixed.startswith("```"):
        lines = fixed.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fixed = "\n".join(lines)
    return _sanitize_generated_code(fixed)


def _validation_commands(filename: str) -> list[tuple[str, list[str]]]:
    commands = [
        (
            "py_compile",
            [sys.executable, "-m", "py_compile", filename],
        )
    ]

    smoke_imports = {
        "router.py": ["router", "orchestrator", "model_router", "browser", "terminal"],
        "browser.py": ["browser", "router"],
        "model_router.py": ["model_router", "brains.brain", "brains.brain_claude", "brains.brain_ollama"],
        "brains/brain.py": ["brains.brain"],
        "brains/brain_claude.py": ["brains.brain_claude"],
        "brains/brain_ollama.py": ["brains.brain_ollama"],
        "brains/brain_gemini.py": ["brains.brain_gemini"],
        "self_improve.py": ["self_improve", "evals"],
        "api.py": ["api", "router", "model_router"],
        "memory.py": ["memory"],
        "terminal.py": ["terminal"],
        "config.py": ["config", "model_router"],
        "vault.py": ["vault", "wiki_builder"],
        "wiki_builder.py": ["wiki_builder", "vault"],
        "source_ingest.py": ["source_ingest", "vault"],
        "skill_factory.py": ["skill_factory", "skills", "vault"],
        "skills.py": ["skills"],
        "local_runtime/local_beta.py": ["local_runtime.local_beta"],
        "local_runtime/local_model_automation.py": ["local_runtime.local_model_automation"],
        "local_runtime/local_model_benchmark.py": ["local_runtime.local_model_benchmark"],
        "local_runtime/local_model_eval.py": ["local_runtime.local_model_eval"],
        "local_runtime/local_stt.py": ["local_runtime.local_stt"],
        "local_runtime/local_training.py": ["local_runtime.local_training"],
        "local_runtime/local_tts.py": ["local_runtime.local_tts"],
    }
    modules = smoke_imports.get(filename)
    if modules:
        commands.append(
            (
                "import_smoke",
                [sys.executable, "-c", f"import {', '.join(modules)}; print('ok')"],
            )
        )
    return commands


def _parity_commands(filename: str) -> list[tuple[str, list[str]]]:
    commands = []
    parity_scripts = {
        "router.py": (
            "import orchestrator; "
            "d=orchestrator.classify('build the vault wiki'); assert d.tool=='knowledge'; "
            "d=orchestrator.classify('create a skill from the vault about local markdown knowledge'); assert d.tool=='skill'; "
            "print('ok')"
        ),
        "orchestrator.py": (
            "import orchestrator; "
            "d=orchestrator.classify('refresh the vault index'); assert d.tool=='knowledge'; "
            "d=orchestrator.classify('promote repeated eval failures into a skill'); assert d.tool=='skill'; "
            "print('ok')"
        ),
        "vault.py": (
            "import vault; "
            "s=vault.status(); assert 'doc_count' in s and 'wiki_page_count' in s; "
            "print('ok')"
        ),
        "wiki_builder.py": (
            "from wiki_builder import build_wiki; "
            "result=build_wiki(); assert 'page_count' in result and 'index_doc_count' in result; "
            "print('ok')"
        ),
        "source_ingest.py": (
            "import source_ingest; "
            "result=source_ingest.ingest_source('README.md', auto_build=False, dry_run=True); assert result.get('ok'); "
            "print('ok')"
        ),
        "skill_factory.py": (
            "import skill_factory; "
            "result=skill_factory.create_skill_from_vault('local markdown knowledge', dry_run=True); assert result.get('ok'); "
            "print('ok')"
        ),
        "skills.py": (
            "import skills; "
            "assert len(skills.all_skills()) >= 1; "
            "print('ok')"
        ),
        "self_improve.py": (
            "import self_improve; "
            "assert callable(self_improve.self_review); "
            "assert callable(self_improve.review_text); "
            "assert callable(self_improve.self_improve); "
            "review=self_improve.self_review(); "
            "assert isinstance(review, dict) and 'ok' in review; "
            "text=self_improve.review_text(review); "
            "assert isinstance(text, str) and text.strip(); "
            "print('ok')"
        ),
    }
    script = parity_scripts.get(filename)
    if script:
        commands.append(("parity", [sys.executable, "-c", script]))
    return commands


def _run_validation(filename: str) -> dict:
    results = []
    for name, command in _validation_commands(filename) + _parity_commands(filename):
        proc = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout + proc.stderr).strip()
        results.append({
            "name": name,
            "ok": proc.returncode == 0,
            "output": output,
        })
        if proc.returncode != 0:
            break

    return {
        "ok": all(item["ok"] for item in results),
        "checks": results,
        "summary": "; ".join(
            f"{item['name']}={'ok' if item['ok'] else 'failed'}" +
            (f" ({item['output'][:160]})" if item["output"] else "")
            for item in results
        ),
    }


def self_improve(instruction: str = None, filename: str = None) -> dict:
    """
    Full self-improvement pipeline:
    1. Analyze code to find what to improve (if no instruction given)
    2. Generate improved code using Opus
    3. Back up original
    4. Apply changes
    5. Return summary for user approval/review

    Returns dict with: file, backup, diff, summary
    """
    evidence_bundle = None

    # Step 1: figure out what to improve
    if not instruction:
        evidence_bundle = evals.build_improvement_brief(area=filename)
        if not evidence_bundle.get("ok"):
            return {
                "error": evidence_bundle.get("reason", "Not enough recent eval evidence to justify self-improvement."),
                "action_needed": "log failures via /feedback or trigger reproducible runtime failures first",
            }
        filename = filename or evidence_bundle["target_file"]
        instruction = evidence_bundle["instruction"]
    elif not filename:
        # Try to infer filename from instruction
        for f in IMPROVABLE_FILES:
            name = f.replace(".py", "")
            if name in instruction.lower() or f in instruction.lower():
                filename = f
                break
        if not filename:
            filename = "router.py"  # default to routing improvements

    print(f"[Self-Improve] Improving {filename}: {instruction[:80]}...")

    # Step 2: generate improved code
    new_code = generate_improvement(filename, instruction, evidence_bundle=evidence_bundle)

    # Step 3 & 4: backup and apply
    try:
        backup_path, diff = apply_improvement(filename, new_code)
    except Exception as e:
        first_error = str(e)
        if "syntax validation" in first_error.lower() or "invalid syntax" in first_error.lower():
            try:
                repaired_code = repair_generated_code(filename, new_code, first_error)
                repaired_code = _heuristic_comment_fix(repaired_code, first_error)
                backup_path, diff = apply_improvement(filename, repaired_code)
            except Exception as repair_error:
                second_error = str(repair_error)
                if "syntax validation" in second_error.lower() or "invalid syntax" in second_error.lower():
                    try:
                        repaired_again = repair_generated_code(filename, repaired_code, second_error)
                        repaired_again = _heuristic_comment_fix(repaired_again, second_error)
                        backup_path, diff = apply_improvement(filename, repaired_again)
                    except Exception as final_error:
                        return {
                            "error": f"Self-improve aborted before applying changes: {final_error}",
                            "file": filename,
                            "instruction": instruction,
                            "initial_error": first_error,
                            "repair_error": second_error,
                        }
                else:
                    return {
                        "error": f"Self-improve aborted before applying changes: {repair_error}",
                        "file": filename,
                        "instruction": instruction,
                        "initial_error": first_error,
                    }
        else:
            return {
                "error": f"Self-improve aborted before applying changes: {e}",
                "file": filename,
                "instruction": instruction,
            }

    validation = _run_validation(filename)
    if not validation["ok"]:
        restore_backup(os.path.basename(backup_path))
        evals.log_failure(
            issue=f"Self-improve validation failed for {filename}.",
            expected="Generated change should compile and pass smoke validation.",
            response=validation["summary"],
            source="self_improve_validation",
        )
        return {
            "error": f"Self-improve reverted after validation failed: {validation['summary']}",
            "file": filename,
            "instruction": instruction,
            "backup": os.path.basename(backup_path),
            "validation": validation,
            "evidence_ids": evidence_bundle.get("failure_ids", []) if evidence_bundle else [],
        }

    result = {
        "file": filename,
        "backup": os.path.basename(backup_path),
        "diff": diff,
        "instruction": instruction,
        "lines_changed": diff.count("\n+") + diff.count("\n-"),
        "validation": validation,
        "evidence_ids": evidence_bundle.get("failure_ids", []) if evidence_bundle else [],
    }
    evals.record_improvement(result)
    return result


def restart_jarvis() -> None:
    """Restart the Jarvis process to load updated code."""
    print("[Self-Improve] Restarting Jarvis to apply changes...")
    subprocess.Popen([sys.executable] + sys.argv)
    sys.exit(0)
