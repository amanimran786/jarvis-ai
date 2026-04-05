Name: Local Model Tuning

Purpose:
Improve Jarvis's local-model path through selective dataset export, targeted teacher distillation, and tuned Ollama model targets.

Rules:
- Prefer exporting strong successful interactions before spending on teacher rewrites.
- Use paid distillation only for repeated failures or high-value weak local answers.
- Treat the tuned local model as the default general model once it exists, but keep the coder model separate for code-heavy tasks.
- Be explicit that small local models can improve materially without becoming identical to frontier hosted models.
- Keep the pipeline inspectable: datasets, distilled examples, and Modelfiles should all be local files.
