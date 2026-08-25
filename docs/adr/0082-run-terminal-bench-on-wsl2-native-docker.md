# ADR 0082: Run Terminal-Bench on WSL2 native Docker

Status: Accepted

Date: 2026-08-22

Terminal-Bench 2.1 preflight and scored execution use the native Docker Engine inside an Ubuntu WSL2 distribution whose ext4 virtual disk is stored on D:, because repeated long-lived trials exposed Docker Desktop Linux Engine API failures after otherwise successful preflight. Windows CMD remains a delegating entrypoint, project source and durable evidence remain under `/mnt/d/agent_learning/cc-harness`, and mutable Docker, Harbor and task workspace state stays on the distribution's ext4 filesystem. The migration keeps a verified rollback export; Docker Desktop remains installed but isolated from Ubuntu and only ownership-proven Terminal-Bench resources are removed after WSL-native validation. Images required by the frozen 89-task catalog are transferred locally only after digest verification, with network pulls limited to missing layers. Interrupted scored work resumes only when the same logical trial and workspace continuity are provable; otherwise it remains `infrastructure_pending` without another scored model attempt.
