# Config Manifest — Real Source of Truth

> **Real manifest (source of truth).** Fill this file manually with all environment variables, secrets and runtime config.
> The AI reverse-engineered manifest lives in [config-manifest_reverse.md](config-manifest_reverse.md) and is generated after implementation by scanning the codebase.

## 1. Layers — where configuration lives

TODO: Describe each layer (keys.env, Secret Manager, Firestore, env vars).

| # | Layer | Storage | Loaded at | Typical keys |
|---|-------|---------|-----------|--------------|
| 1 | Bootstrap input | `keys.env` (gitignored) | local scripts | `PROJECT_ID` |
| 2 | Secrets | Secret Manager | cold start | `API_ID` |
| 3 | Function env vars | Cloud Function | boot | `GCP_PROJECT_ID` |
| 4 | Runtime config | Firestore | every start | `telegram/keywords` |

## 2. Variable registry

TODO: Hard = required, Opt = optional.

| Variable | Layer | Level | Default | Consumer | Why |
|----------|-------|-------|---------|----------|-----|
| `EXAMPLE_VAR` | 1 | Hard | — | `main.py` | TODO |

## 3. Naming / convention reference

TODO

## 4. Environment setup checklist

TODO

## 5. Anti-drift verification

TODO

## 6. Known quirks

TODO
