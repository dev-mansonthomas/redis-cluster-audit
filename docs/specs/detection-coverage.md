# Spec & Plan — Detection coverage + missing detections

**Status:** Phase A & B complete (35 unit tests green + Docker E2E)
**Branch:** `fix/code-review-fixes` (Phase 1 code-review fixes already implemented here; this
is the next feature on top). Nothing committed yet.
**Date:** 2026-08-19
**Owner:** Thomas (Redis SA)

> This document is the durable source of truth for this feature so it survives any context
> compaction. Keep the **Progress tracker** at the bottom up to date as tasks land.

---

## 1. Goal & context

`audit.py` is a read-only auditor for **existing/external** Redis servers; the Docker cluster
(`docker/`, `seed/seed.py`, `run.sh`) is an **internal test fixture** for the tool itself.

Two gaps were identified:

- **(#2) Coverage gap** — detections `audit.py` *already has* but the fixture never triggers, so
  those code paths are untested.
- **(#3) Capability gap** — high-impact / security bad practices the tool *does not detect at all*.

This feature closes both, test-first. Unit tests on the pure detection functions are the source
of truth (fast, deterministic, no Docker); the fixture is extended to *demonstrate* the findings,
verified by one manual Docker E2E (not gated in CI). New recommendations render for free through
the existing `section_recommendations` → `generate_recommendations` path.

**No new libraries.** New detections use existing redis-py APIs (`CLIENT LIST` `user` field —
confirmed via Context7; `LATENCY LATEST`, `SLOWLOG`, INFO, `CONFIG GET` already collected).
`HOTKEYS` requires Redis ≥ 8.6 but the fixture is 6.2.20, so hot-key detection uses a slowlog
heuristic; `HOTKEYS` is noted as a future upgrade path.

---

## 2. What the fixture simulates today (baseline)

Config (`docker/docker-compose.yml`): `timeout 0`, `maxmemory 1gb` + `maxmemory-policy noeviction`,
`save ""` (no RDB) + no AOF, `slowlog-log-slower-than 10000`, no `bind`, no auth on `default`,
`--cluster-replicas 0`. Data (`seed/seed.py`): big keys (string 1 MB/100 KB/10 KB, hash 2000,
list 5000, set 2000), ~63 % keys without TTL, slow O(N) commands (HGETALL/SMEMBERS/LRANGE).

Findings already triggered: `timeout=0` (CRIT), default `nopass`+`@all` (CRIT/HIGH), bind-all
(HIGH), protected-mode off (HIGH), no TLS (MED), no persistence (MED), no replicas (HIGH), big
keys (MED), >50 % no TTL (MED), slow log entries.

---

## 3. Gap analysis

### (#2) Existing detections NOT triggered by the fixture — exact conditions

All live in `audit.py`. Coverage = a unit test asserting each fires, + fixture extension.

| ID | Detection | Location / condition |
|----|-----------|----------------------|
| C1 | `maxmemory=0` (no cap → OOM) | `generate_recommendations`: `nd["config"].get("maxmemory") in ("0", 0)` |
| C2 | named user `+@all` (unrestricted) | `analyse_security`: `user["enabled"] and user["has_all_commands"] and name != "default"` |
| C3 | named user `+@dangerous` | `analyse_security`: `has_dangerous and not has_all_commands and name != "default"` |
| C4 | named user write + `~*` | `analyse_security`: `has_write and has_all_keys and name != "default"` |
| C5 | ACL not configured (no users) | `analyse_security`: `if not users` per node |
| C6 | ACL LOG auth failures > 5 | `analyse_security`: `len(auth_failures) > 5` (reason in `auth`/`noauth`) |
| C7 | evictions > 0 | `generate_recommendations`: `stats["total_evictions"] > 0` |
| C8 | hit ratio < 0.80 | `generate_recommendations`: `stats["hit_ratio"] < 0.80` |
| C9 | idle connections > 10 min (> 10) | `generate_recommendations`: `conn_analysis["idle_buckets"][">10min"] > 10` |
| C10 | slowlog threshold too permissive | `generate_recommendations`: `int(threshold) > 100_000` |

### (#3) Missing detections to add (with proposed thresholds — tunable)

| ID | New detection | Severity | Rule |
|----|---------------|----------|------|
| N1 | `noeviction` on a capped cache | MEDIUM | `maxmemory != 0` and `maxmemory-policy == "noeviction"` |
| N2 | app connects as `default`/admin | HIGH | any `CLIENT LIST` entry with `user == "default"` from a non-loopback `addr` |
| N3 | connection concentration on one IP | MEDIUM | a single source IP holds > `CONN_PER_IP_WARN` (default 200) connections |
| N4 | monolithic-String data model | MEDIUM | `string` ≥ 90 % of sampled types **and** largest big-key is a string ≥ 100 KB |
| N5 | high memory fragmentation | MEDIUM | any node `mem_fragmentation_ratio > 1.5` |
| N6 | latency blindness / spikes | LOW / MED | `latency-monitor-threshold == 0` (blind) OR any `LATENCY LATEST` event |
| N7 | AOF + RDB both enabled (info) | LOW | `appendonly == "yes"` and `save` non-empty (fork+fsync overhead on a pure cache) |
| N8 | hot-key heuristic (stretch) | MEDIUM | one key in ≥ 50 % of slowlog entries **and** ≥ small absolute count; prefer `HOTKEYS` on Redis ≥ 8.6 |

### Out of scope (not observable by a read-only server-side audit)
`KEYS *` in prod, client-side N+1 / missing pipelining, weak passwords (stored hashed). Document as
limitations; do not attempt to simulate.

---

## 4. TDD task plan

### Phase A — cover existing detections (#2)
Characterization tests (green-first: they lock that each path works); then extend the fixture.

- **A1** — `tests/test_recommendations.py` (new): assert `generate_recommendations` emits C1, C7,
  C8, C9, C10 from minimal fabricated `all_node_data / conn_analysis / keyspace / stats`. Reuses
  `audit.generate_recommendations`. (If any is red → latent bug, fix it.)
- **A2** — extend `tests/test_security.py`: assert `analyse_security` emits C2, C3, C4, C5, C6.
  Reuse the `make_user` fixture in `tests/conftest.py`.
- **A3** — extend the fixture to trigger A1/A2 live: heterogeneous per-node memory in
  `docker/docker-compose.yml` (node-1 `--maxmemory 0`; node-2 `8mb` + `noeviction`; node-3 `8mb`
  + `allkeys-lru` overfilled → evictions); `run.sh` creates an over-privileged `app_user`
  (`~* +@all`) via the stdin-pipe pattern and issues a few failed `AUTH` (→ ACL-LOG auth
  failures); `seed.py` GETs missing keys (→ hit ratio < 80 %). Verify via Docker E2E.
  `idle>10min` (C9) is impractical live → unit-tested only; document it.

### Phase B — add missing detections (#3), red → green
Each new reco flows through `generate_recommendations` → renders automatically.

- **B1** — N1 (`noeviction` capped). Test in `tests/test_recommendations.py`; impl in
  `audit.py:generate_recommendations`.
- **B2** — N2 (app as `default`). Test in `tests/test_connections.py` (new); extend
  `audit.py:analyse_connections` to capture `c.get("user")`, add reco in `generate_recommendations`.
- **B3** — N3 (per-IP concentration). Test in `tests/test_connections.py`; reco from existing
  `conn_analysis["per_ip"]`.
- **B4** — N4 (monolithic-String model). Test in `tests/test_keyspace_model.py` (new); reco from
  `keyspace["type_distribution"]` + `big_keys`.
- **B5** — N5 (fragmentation). Test in `tests/test_recommendations.py`; reco from `nd["info"]`
  `mem_fragmentation_ratio`.
- **B6** — N6 (latency). Add `latency-monitor-threshold` to `collect_config` key list; test + reco.
- **B7** — N7 (AOF+RDB, informational). Test + reco.
- **B8** — N8 (hot-key heuristic, stretch). Test both fire and no-fire; reco from per-node
  `slowlog`. Comment: prefer `HOTKEYS` when auditing Redis ≥ 8.6.
- **B9** — extend the fixture to demonstrate B1–B8 (noeviction node; a client held open as
  `default` during the audit; a large monolithic string; a hammered hot key; skew
  `latency-monitor-threshold`). Verify via Docker E2E.

---

## 5. Meta

- **Files created:** `tests/test_recommendations.py`, `tests/test_connections.py`,
  `tests/test_keyspace_model.py`, this spec.
- **Files modified:** `audit.py` (`generate_recommendations`, `analyse_connections`,
  `collect_config`), `tests/test_security.py`, `docker/docker-compose.yml`, `run.sh`,
  `seed/seed.py`.
- **Riskiest steps & de-risk:**
  - B2 depends on the `CLIENT LIST` `user` field (confirmed via Context7; also assert live in E2E).
  - B8 heuristic risks false positives → conservative threshold (share **and** absolute count),
    unit-test both directions.
  - A3/B9 heterogeneous per-node memory could destabilize cluster formation → unit tests stay the
    source of truth; fixture is demonstration only, verified by one E2E.
- **Run tests:** `python -m pytest -q` (session venv:
  `/private/tmp/claude-502/-Users-thomas-manson-Projects-redis-cluster-audit/7845566d-ef32-432e-a73b-5f3b09cee66e/scratchpad/venv`;
  recreate with `python3 -m venv` + `pip install -r requirements-dev.txt` if the scratchpad is gone).
- **Done =** every existing detection has a unit test (Phase A); every new detection is red→green
  and renders in the report; Docker E2E shows old + new findings in `report.html`;
  `shellcheck run.sh` clean.
- **Sequencing:** Phase A (A1→A2→A3) first — low-risk, closes coverage. Then Phase B (B1→…→B9).
  Each task is independently committable.

---

## 6. Progress tracker

Phase A — coverage of existing detections (#2) — ✅ DONE
- [x] A1 — `tests/test_recommendations.py`: C1, C7, C8, C9, C10 (6 tests)
- [x] A2 — extend `tests/test_security.py`: C2, C3, C4, C5, C6 (+ C6 aggregated-count)
- [x] A3 — fixture triggers live + Docker E2E. **Live-verified:** C1 (node-1 `maxmemory 0`),
  C2 + C4 (`app_user ~* +@all`), C6 (6 failed AUTHs), C8 (hit ratio 9.6%), C10 (node-1
  `slowlog 200000`). **Unit-only (impractical/unsafe to simulate live):** C3, C5, C7, C9.
- [x] Bonus fix (found via E2E): C6 now sums the ACL LOG `count` field instead of counting
  entries — ACL LOG aggregates identical failures, so a one-source brute-force previously
  slipped past. `audit.py:analyse_security`.

Phase B — new detections (#3) — ✅ DONE (35 unit tests green)
- [x] B1 — N1 noeviction on capped cache (live-verified)
- [x] B2 — N2 app connects as `default` (unit-only: local cluster is all-loopback)
- [x] B3 — N3 per-IP connection concentration (unit-only: all-loopback)
- [x] B4 — N4 monolithic-String data model (unit-only: fixture is ~86% string, under the 90% bar)
- [x] B5 — N5 high fragmentation (live-verified, ratio 3.97)
- [x] B6 — N6 latency monitor disabled / spikes (live-verified: monitor disabled)
- [x] B7 — N7 AOF + RDB both (live-verified on node-3; needs unquoted `--save 3600 1`)
- [x] B8 — N8 hot-key heuristic from slow log (live-verified: one key = 20/23 slow ops)
- [x] B9 — fixture demonstrates N1/N5/N6/N7/N8 live + Docker E2E

**New config knobs:** `CONN_PER_IP_WARN` (default 200); `collect_config` now also reads
`latency-monitor-threshold`. `analyse_connections` returns `default_user_external`.

---

## 7. Related context

- Phase 1 (code-review fixes) on the same branch: pipelined scan, per-node `analyse_security`,
  `require_full_coverage=False`, `db0`/Has-TTL report fixes, hardened canary, single-source ACL
  (`audit.py --print-acl`), self-contained `run.sh`, latest client libs (redis-py 8.1.0,
  python-dotenv 1.2.3), Python floor 3.10. 12 unit tests green + Docker E2E validated.
- Test harness: `pyproject.toml` (`pythonpath=["."]`, `requires-python>=3.10`),
  `requirements-dev.txt`, `tests/conftest.py` (`FakeNode`, `_FakePipeline`, `make_user`,
  `secure_config`).

## 8. Follow-up shipped — HOTKEYS opt-in mode

Branch `feat/hotkeys-detection` (off `main`): an **invasive, opt-in** hot-key mode.
`audit.py --hotkeys N` runs `HOTKEYS START/GET/STOP/RESET` for N seconds per node on
Redis ≥ 8.6, renders a Hot Keys report section (per-key CPU/network share), and adds
recommendations when a key dominates (≥ 50%). The default audit stays read-only (slow-log
heuristic). Key facts, verified against redis:8.6.5: `HOTKEYS` is a stateful tracking
container command (not a read-only query — it mutates tracking state); needs a user granted
`+HOTKEYS`; no LFU policy required; `GET` reply is `[{... 'by-cpu-time-us':[k,v,...],
'by-net-bytes':[k,v,...] ...}]`. Version-gated via `version_at_least`. 17 unit tests +
a live-8.6 integration check; nodes < 8.6 are skipped.
