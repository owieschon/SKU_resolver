# Migration Notes — src-layout consolidation (2026-06-06)

> **Historical record.** The catalog was later regenerated from the grammar.
> Current live state: `data/catalog.csv` holds **9,986 unique SKUs** (of which
> the audit derives **9,487** active, resolvable SKUs at runtime after excluding
> obsolete/battery rows), and `data/known_construct_truncations.json` reports
> **0** pinned truncations (grammar SKUs round-trip by construction). The raw
> **9,919-row** catalog and the **64-truncation** figures below describe the
> pre-regeneration catalog and are kept only as a point-in-time migration log.

Source: the originating SKU-parser project (out of repo) (flat
directory with a `sku_translator -> .` self-symlink to make it importable).
Every discrepancy between documented and observed state was reconciled at
point of use, not assumed. Findings, in severity order:

## 1. Latent silent-degradation import bug (FIXED)

`extractor.py` lazily imported the grammar by its **top-level** module name
(`import_module('part_number_parser')`) inside a swallowed `except
ImportError`. That name resolves only when the cwd contains the module file —
true in the original flat directory, false in any packaged deployment. In any
other layout the canonical-SKU passthrough **silently disabled** and the
extractor degraded to token analysis. Caught by `constructor.py`'s
import-time round-trip selftest during this migration; fixed with a
package-qualified import (flat-module fallback retained). The bug was present
since 2026-05-11 and masked by always running from inside the source
directory.

## 2. Construct-path truncations pinned (pre-regeneration: 64, of which 26 dangerous)

> **Superseded by the later catalog regeneration.** The figures in this section
> describe the pre-regeneration catalog. The current catalog is generated from
> the grammar and round-trips by construction, so
> `data/known_construct_truncations.json` now reports **0** pinned truncations.
> Kept as a point-in-time migration log.

At migration time, the round-trip audit (`scripts/roundtrip_audit.py`) surfaced
64 catalog SKUs whose `extract -> construct` chain truncated to an embedded base
SKU (e.g. `'FB-4ZN SADDLE'` constructed as `'FB-4ZN'`). **26 of the 64 truncated
to a different real catalog SKU** — the dangerous class, since a free-text input
could assemble to a real-but-wrong part. All 64 still resolved correctly through
`translate()` (the verbatim path matches the full string first; the identity
gate covered every active SKU). They were pinned in
`data/known_construct_truncations.json` so the audit would fail on any NEW
entry. A grammar-level fix (refuse construction when unconsumed tokens remain)
was deliberately out of scope for that slice — it changes free-text resolution
behavior and needs its own regression pass. The subsequent regeneration of the
catalog from the grammar removed these construct-path cases entirely (pin set
now empty).

## 3. Stale documented numbers (corrected)

| Claim (handoff/README, 2026-05-11) | Observed (2026-06-06) | Resolution |
|---|---|---|
| "14 Python files" | 13 `.py` files on disk | The handoff's own inventory table lists exactly 13; the headline figure counted the self-symlink. Nothing missing. |
| "47-test integration suite" | 62 tests collected, 62 pass | Suite was extended after the handoff was written (file grew 23.6KB → 34.5KB the same day). 62 is authoritative; CI records the collected count. |
| "9,487 active SKUs" | 9,487 derived at runtime | Confirmed. The audit derives the count from the catalog CSV on every run — never hardcoded. |
| "98.6% catalog coverage" | 98.16% decode to a structural pattern; 96.96% fully round-trip | Different denominators: the grammar *decodes* ~44 informational pattern types it never claimed to *reconstruct*. Audit floors: coverage ≥ 95% full round-trip. |

## 4. Dead container paths (removed)

`test_integration.py` and `fixture_catalog.py` carried fallback paths to
`/mnt/user-data/uploads/…` — artifacts of the original build environment.
Replaced with `SKU_CATALOG_PATH` env override + repo-relative default.

## 5. macOS UF_HIDDEN vs Python 3.14 `.pth` processing (root-caused, worked around)

**Symptom:** the editable install worked immediately after `pip install -e .`,
then `import sku_translator` started raising ModuleNotFoundError minutes
later — same interpreter, same shell, no code change.

**Root-cause isolation, in the order it was established:**

1. Editable `.pth` hook present in site-packages with correct content and a
   valid target path — yet the path never appeared on `sys.path`, even via a
   manual `site.addsitedir()`.
2. Read this interpreter's `site.py` rather than guessing: Python 3.14's
   `addpackage()` (site.py:183-186) **skips `.pth` files carrying the macOS
   `UF_HIDDEN` BSD flag** — a recent CPython security hardening.
3. `ls -lO`: the `.pth` file — and the entire `.venv` tree — was flagged
   `hidden`. `chflags nohidden` fixed the import instantly.
4. ~30 minutes later the flag was **back**: something re-applies it.
   Discrimination tests: fresh dotfiles in `/tmp` and in the repo are NOT
   flagged at creation (the flagger is asynchronous), and `~/.zshrc` /
   `~/.gitconfig` are NOT flagged (it is scoped to this Desktop-managed
   area, not system-wide).
5. Older (May 2026) venvs in the same area carry the same flag — and their
   `.pth` files are additionally `dataless` (contents evicted from local
   disk; FileProvider/iCloud eviction). A macOS update (Tahoe 26.5.1) had
   been installed at 13:04 the same day the failure first manifested —
   consistent with a sync rescan or re-enabled Desktop sync as the trigger,
   though which OS version introduced the flag-mirroring is not provable
   from this machine.

**Verdict:** a two-component collision. Neither the OS service marking
dot-prefixed items hidden nor CPython's hidden-`.pth` hardening is wrong in
isolation; together they silently kill editable installs on sync-managed
paths. **Mitigation here:** tests use `pythonpath = ["src"]` in pytest
config and never depend on the `.pth` hook (CI on Linux was never affected).
**Residual risk noted:** a content-evicting sync service operating over a
working tree is a corruption vector for git object stores; the GitHub
remote is the source of truth for this repo.
