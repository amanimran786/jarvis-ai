# JARVIS KNOWLEDGE BASE — PYTHON PATTERNS
# Aman's Python usage, style, and established patterns
# Loaded by: TechAssist module when Python domain is detected
# Privacy tier: Public

---

## CONTEXT
Aman is not a software engineer. He is a T&S/AI Safety operator who writes Python
to solve operational problems faster. His Python is purpose-built and practical:
data processing, automation, tooling, pattern detection, scripting. Not web frameworks
or production services (though he understands them).

---

## STYLE DEFAULTS
- **Readable over clever** — code is documentation
- **Explicit over implicit** — name things what they are
- **Functional where it fits** — list comprehensions, map/filter for simple transforms
- **Scripts first** — most of his Python is scripts, not OOP systems
- **Comments on the why, not the what** — don't explain what Python is doing; explain why

---

## COMMON TOOLCHAIN

```python
# Data and analysis
import pandas as pd
import numpy as np

# HTTP and APIs
import requests
import httpx          # async

# File I/O
import json
import csv
import pathlib

# Pattern matching and text
import re
from collections import Counter, defaultdict

# AI/ML tooling (from Anthropic work)
from anthropic import Anthropic
import openai
from sentence_transformers import SentenceTransformer  # for embeddings
import chromadb       # vector store
```

---

## CORE PATTERNS

### 1. Pattern Clustering (from Anthropic abuse detection work)
```python
from sentence_transformers import SentenceTransformer
from sklearn.cluster import DBSCAN
import numpy as np

def cluster_similar_cases(cases: list[dict], text_field: str, eps: float = 0.3):
    """
    Cluster similar abuse cases by behavioral pattern similarity.
    Returns cases with cluster_id assigned.
    Uses DBSCAN so outliers (novel patterns) are labeled -1.
    """
    model = SentenceTransformer('all-MiniLM-L6-v2')
    texts = [c[text_field] for c in cases]
    embeddings = model.encode(texts, normalize_embeddings=True)

    clustering = DBSCAN(eps=eps, min_samples=2, metric='cosine').fit(embeddings)

    for i, case in enumerate(cases):
        case['cluster_id'] = int(clustering.labels_[i])
        case['is_novel'] = clustering.labels_[i] == -1

    return cases
```

### 2. Data Pipeline / Batch Processing
```python
from pathlib import Path
import json

def process_batch(input_dir: Path, output_dir: Path, processor_fn):
    """
    Process all .json files in input_dir, write results to output_dir.
    Skips already-processed files. Logs failures without stopping.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    processed, failed = 0, 0

    for path in sorted(input_dir.glob("*.json")):
        out_path = output_dir / path.name
        if out_path.exists():
            continue  # idempotent — skip already done

        try:
            with open(path) as f:
                data = json.load(f)
            result = processor_fn(data)
            with open(out_path, 'w') as f:
                json.dump(result, f, indent=2)
            processed += 1
        except Exception as e:
            print(f"FAILED: {path.name} — {e}")
            failed += 1

    print(f"Done. Processed: {processed}, Failed: {failed}")
```

### 3. Anomaly Detection on Metrics
```python
import pandas as pd
import numpy as np

def flag_anomalies(df: pd.DataFrame, metric_col: str,
                   window: int = 7, threshold_stddev: float = 2.0) -> pd.DataFrame:
    """
    Flag rows where metric_col deviates more than threshold_stddev
    from its rolling mean. Used for quality signal monitoring.
    """
    df = df.copy().sort_values('date')
    df['rolling_mean'] = df[metric_col].rolling(window=window, min_periods=3).mean()
    df['rolling_std']  = df[metric_col].rolling(window=window, min_periods=3).std()
    df['z_score'] = (df[metric_col] - df['rolling_mean']) / df['rolling_std'].replace(0, np.nan)
    df['is_anomaly'] = df['z_score'].abs() > threshold_stddev
    return df
```

### 4. API Client Pattern (clean, reusable)
```python
import httpx
from typing import Any

class JarvisAPIClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.timeout = timeout

    def get(self, path: str, **params) -> Any:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(f"{self.base_url}/{path}", headers=self.headers, params=params)
            r.raise_for_status()
            return r.json()

    def post(self, path: str, body: dict) -> Any:
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}/{path}", headers=self.headers, json=body)
            r.raise_for_status()
            return r.json()
```

### 5. Simple Vector Store Wrapper (for Jarvis memory)
```python
import chromadb
from sentence_transformers import SentenceTransformer
from typing import Optional

class MemoryStore:
    def __init__(self, path: str = "./memory/semantic", collection: str = "jarvis"):
        self.client = chromadb.PersistentClient(path=path)
        self.collection = self.client.get_or_create_collection(collection)
        self.model = SentenceTransformer('all-MiniLM-L6-v2')

    def add(self, id: str, text: str, metadata: dict = None):
        embedding = self.model.encode(text).tolist()
        self.collection.upsert(
            ids=[id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata or {}]
        )

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        embedding = self.model.encode(query).tolist()
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=top_k
        )
        return [
            {"id": id_, "text": doc, "metadata": meta, "distance": dist}
            for id_, doc, meta, dist in zip(
                results['ids'][0],
                results['documents'][0],
                results['metadatas'][0],
                results['distances'][0]
            )
        ]
```

---

## APPROACH PROTOCOL FOR PYTHON REQUESTS

1. **State what the script does** in the docstring — one sentence
2. **Explicit types** — use type hints for function signatures
3. **Fail loudly for dev, fail gracefully for prod** — know which mode you're in
4. **No hardcoded secrets** — always use env vars or config
5. **Idempotent where possible** — re-running shouldn't cause double-processing

---

*Python patterns version: 1.0*
*Last updated: April 2026*
