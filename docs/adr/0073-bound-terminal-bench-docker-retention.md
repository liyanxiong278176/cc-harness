# ADR 0073: Bound Terminal-Bench Docker retention

Status: Accepted

Date: 2026-08-18

The Terminal-Bench 2.1 serial run requires at least 80 GB free on Docker's actual storage filesystem and
recommends 100 GB before paid execution. Inspection of the 89 pinned image manifests on 2026-08-18 found
17.8 GB of compressed layers before shared-layer deduplication, with a largest compressed image of 1.334 GB.
Most tasks declare a 10,240 MB writable-storage limit, but concurrency one means those quotas are not allocated
89 times simultaneously. The accepted planning estimate is 35–60 GB of additional peak usage with bounded
cleanup; it is not an exact promise about registry compression or task writes.

Before each task the runner records Docker resource identities and labels new resources. Only after verifier,
trajectory and log evidence is durably published may it stop/remove newly-created task-owned containers, temporary volumes and unused
task-specific image references proven to belong to that run. Shared layers remain reusable. Low space pauses
before launching another task and remains an infrastructure condition. Resume reconciles ownership-proven
orphans only; the runner never invokes a global Docker prune or removes pre-existing resources.
