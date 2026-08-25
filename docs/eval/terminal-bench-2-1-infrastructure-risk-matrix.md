# Terminal-Bench 2.1 infrastructure risk matrix

This is the preflight risk analysis for the pinned 89-task Terminal-Bench 2.1
catalog used by `cc-harness`. It describes conditions that can prevent an
official `pass`/`fail` result; it does not change the benchmark's official
grader or reward.

## Catalog shape

The frozen catalog contains 89 tasks across 16 categories:

| Category | Tasks |
| --- | ---: |
| data-processing | 4 |
| data-querying | 1 |
| data-science | 8 |
| debugging | 5 |
| file-operations | 5 |
| games | 1 |
| machine-learning | 3 |
| mathematics | 4 |
| model-training | 4 |
| optimization | 1 |
| personal-assistant | 1 |
| scientific-computing | 8 |
| security | 8 |
| software-engineering | 26 |
| system-administration | 9 |
| video-processing | 1 |

The categories overlap with operational hazards. The following groups are
name-based triage sets, not a claim that every member will fail.

| Risk surface | Count | Representative catalog tasks |
| --- | ---: | --- |
| Docker/build/compiler | 13 | `build-cython-ext`, `build-pmars`, `build-pov-ray`, `cobol-modernization`, `compile-compcert`, `make-doom-for-mips`, `make-mips-interpreter`, `modernize-scientific-stack`, `openssl-selfsigned-cert`, `polyglot-c-py`, `polyglot-rust-c`, `sqlite-db-truncate`, `sqlite-with-gcov` |
| Services/network/process lifecycle | 9 | `cancel-async-tasks`, `configure-git-webserver`, `headless-terminal`, `kv-store-grpc`, `mailman`, `nginx-request-logging`, `pypi-server`, `qemu-alpine-ssh`, `qemu-startup` |
| Dataset/model/download | 15 | `caffe-cifar-10`, `count-dataset-tokens`, `extract-moves-from-video`, `hf-model-inference`, `model-extraction-relu-logits`, `mteb-leaderboard`, `mteb-retrieve`, `pytorch-model-cli`, `pytorch-model-recovery`, `reshard-c4-data`, `sam-cell-seg`, `torch-pipeline-parallelism`, `torch-tensor-parallelism`, `train-fasttext`, `video-processing` |
| Long compute/timeout | 16 | `constraints-scheduling`, `distribution-search`, `dna-assembly`, `dna-insert`, `feal-differential-cryptanalysis`, `feal-linear-cryptanalysis`, `largest-eigenval`, `llm-inference-batching-scheduler`, `mcmc-sampling-stan`, `portfolio-optimization`, `protein-assembly`, `query-optimize`, `raman-fitting`, `torch-pipeline-parallelism`, `torch-tensor-parallelism`, `train-fasttext` |
| File/path/encoding | 21 | `break-filter-js-from-html`, `configure-git-webserver`, `extract-elf`, `filter-js-from-html`, `fix-git`, `gcode-to-text`, `git-leak-recovery`, `git-multibranch`, `large-scale-text-editing`, `log-summary-date-ranges`, `model-extraction-relu-logits`, `nginx-request-logging`, `openssl-selfsigned-cert`, `regex-chess`, `regex-log`, `sanitize-git-repo`, `sqlite-db-truncate`, `sqlite-with-gcov`, `train-fasttext`, `vulnerable-secret`, `write-compressor` |
| Platform/package/runtime | 9 | `cobol-modernization`, `fix-ocaml-gc`, `install-windows-3.11`, `mcmc-sampling-stan`, `qemu-alpine-ssh`, `qemu-startup`, `rstan-to-pystan`, `schemelike-metacircular-eval`, `tune-mjcf` |

## Failure classes and controls

| Failure class | Example symptom | Control before model calls | Formal-run behavior |
| --- | --- | --- | --- |
| Verifier closure/dependency | `ModuleNotFoundError`, missing `ctrf`, `exceptiongroup`, `typing_extensions`, or `tomli` | Frozen offline wheelhouse, import check, and non-scoring verifier smoke | Never enters a paid task until repaired |
| Host Harbor dependency/import | Harbor fails before Docker with a missing host module such as `aiosqlite` | Pinned `uvx` host-agent import check before any task container | Preflight fails with zero model calls |
| Verifier launch/data | Missing or non-executable `/tests/test.sh`, invalid shell syntax, missing task data | Required file/syntax check and smoke in a fresh container | Preflight gate blocks the whole selected run |
| UV/tokenizer bootstrap | `uvx`/`uv` missing, wrong wheel, `tiktoken` cache absent or hash mismatch | Frozen Linux bootstrap archives and SHA checks | Environment is not ready; no model call |
| Docker/storage | Daemon unavailable, image cannot start, disk/inode exhaustion, port conflict | Docker health, storage check, install-only image build, smoke | Bounded pre-model retry; otherwise no formal run |
| OS/compiler/runtime | `gcc`, `qemu`, system library, shell, or interpreter missing | Install-only task container plus smoke output classification | Environment-not-ready evidence, not a model score |
| Network/provider | DNS/TLS/502/reset/5xx while installing required task data | One host-side bootstrap, task smoke, bounded retries/backoff | Transient retry budget; persistent task stays infrastructure-pending |
| Long command false hang | Quiet compile/train/service while process and files are active | Process/CPU/child/file/socket activity heartbeat and class idle budgets | Official total task timeout remains unchanged |
| Harbor evidence/state | No auditable job, malformed completion, interrupted attempt, inconsistent resume | Immutable contract, launch evidence, task checkpoints, same-command resume | Separate infrastructure-pending evidence; no invented reward |
| Ordinary task failure | Verifier runs and reports reward 0 after the agent's attempt | Official verifier result and logs | Recorded as official `fail` |

## Gate and evidence contract

The formal launcher requires the zero-model check, official oracle, synthetic
canary, and a complete task-level prewarm. The prewarm summary must report
`docker_image_started`, `verifier_imports`, `test_sh_syntax_and_executable`,
`verifier_smoke`, `tiktoken_cache`, `data_disk_network`, and `timeout_config`
as true for every selected task, with zero model calls and matching frozen
wheel identity. The smoke is diagnostic only; the official report retains
only Harbor's task `pass`/`fail` outcomes as benchmark scores.
