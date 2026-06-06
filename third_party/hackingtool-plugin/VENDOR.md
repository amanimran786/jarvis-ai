# Vendor: AKCodez/hackingtool-plugin

Source: https://github.com/AKCodez/hackingtool-plugin
Pinned commit: **TODO — pin to a specific commit SHA before production use**

## Vendoring instructions

```bash
# One-time setup — pin to a specific commit, never a branch tip
git subtree add \
  --prefix=third_party/hackingtool-plugin \
  https://github.com/AKCodez/hackingtool-plugin \
  <COMMIT_SHA> --squash
```

## What Jarvis uses from this plugin

Jarvis does NOT invoke the plugin's own runner (`hackingtool`, `launcher.sh`,
or any Docker-based runner). We call the underlying tools directly.

The vendored source is kept here for:
- Auditing what each tool does before enabling it
- Pinning to a reviewed commit
- Offline reference

## Enabled tools (allowlisted in tools/security/hackingtool_adapter.py)

| Tool | Mode | Approval required |
|---|---|---|
| gitleaks | passive — local path scan | Yes |
| trufflehog | passive — local filesystem scan | Yes |
| dnstwist | passive — OSINT typosquatting | Yes |
| subfinder | passive — OSINT subdomain enum | Yes |
| amass | passive — OSINT DNS enum | Yes |
| testssl.sh | active — TLS inspection | Yes |
| wafw00f | active — WAF detection | Yes |
| httpx | active — HTTP probing | Yes |
| nmap | active — port/service/NSE assessment scan on owned hosts; allows `-T0`-`-T5` plus `safe`, `default`, `vuln`, `exploit`, `intrusive`, `malware`, and `fuzzer` script category expressions | Yes |

## Blocked categories (never enabled)

Post-exploitation, credential attacks, wireless attacks, MITM, phishing,
DDoS, payload generation, privileged Docker, sudo escalation. Nmap `brute`
and `dos` script categories stay blocked; payload/callback and sudo/privileged
flags stay blocked rather than approval-gated.

Approval requires both `approved_by` and a valid `approval_token`. If
`JARVIS_SECURITY_APPROVAL_TOKEN` is configured, the token must match that
session secret; otherwise the adapter accepts only the deliberate fallback
phrase used by the local approval UI.

See `tools/security/hackingtool_adapter.py` for the full blocklist.
