#!/usr/bin/env node

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

// Keep each npm release reproducible. Bump this ref together with the npm
// version when publishing a new release; users can override it explicitly.
const DEFAULT_CORE_REF = "5bdd0e83192b67f4d942ed82f39ea3378bf45ac7";
const DEFAULT_GIT_SOURCE =
  `git+https://github.com/liyanxiong278176/cc-harness.git@${DEFAULT_CORE_REF}`;
const DEFAULT_ARCHIVE_SOURCE =
  `https://github.com/liyanxiong278176/cc-harness/archive/${DEFAULT_CORE_REF}.zip`;

function commandExists(command) {
  const probe = process.platform === "win32" ? "where.exe" : "which";
  return spawnSync(probe, [command], { stdio: "ignore" }).status === 0;
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.error) {
    return { status: null, error: result.error };
  }
  return { status: result.status ?? 1, error: null };
}

function exitFrom(result) {
  if (result.error) {
    return null;
  }
  return result.status;
}

function pythonCandidates() {
  return process.platform === "win32"
    ? ["python", "py"]
    : ["python3", "python"];
}

function findPython() {
  return pythonCandidates().find(commandExists) || null;
}

function coreSource() {
  return process.env.CC_HARNESS_CORE_SOURCE || DEFAULT_GIT_SOURCE;
}

function archiveSource() {
  return process.env.CC_HARNESS_CORE_ARCHIVE || DEFAULT_ARCHIVE_SOURCE;
}

function runtimeDirectory() {
  const root = process.platform === "win32"
    ? process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local")
    : process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache");
  return path.join(root, "cc-harness", "npm-runtime");
}

function venvPython(venv) {
  return process.platform === "win32"
    ? path.join(venv, "Scripts", "python.exe")
    : path.join(venv, "bin", "python");
}

function venvExecutable(venv) {
  return process.platform === "win32"
    ? path.join(venv, "Scripts", "cc-harness.exe")
    : path.join(venv, "bin", "cc-harness");
}

function readMarker(marker) {
  try {
    return fs.readFileSync(marker, "utf8").trim();
  } catch {
    return "";
  }
}

function writeMarker(marker, source) {
  fs.mkdirSync(path.dirname(marker), { recursive: true });
  fs.writeFileSync(marker, `${source}\n`, "utf8");
}

function runWithUv(args) {
  const source = coreSource();
  return run("uv", [
    "tool",
    "run",
    "--from",
    source,
    "cc-harness",
    ...args,
  ]);
}

function runWithPython(args) {
  const python = findPython();
  if (!python) {
    console.error(
      "cc-harness 需要 Python 3.11+ 或 uv。请安装 uv 后重试，或者安装 Python 3.11+。",
    );
    return { status: 1, error: null };
  }

  const venv = runtimeDirectory();
  const pythonPath = venvPython(venv);
  const executable = venvExecutable(venv);
  const marker = path.join(venv, ".core-source");
  const source = process.env.CC_HARNESS_CORE_SOURCE || archiveSource();
  const forceInstall = /^(1|true|yes)$/i.test(
    process.env.CC_HARNESS_NPM_REINSTALL || "",
  );

  if (!fs.existsSync(pythonPath)) {
    fs.mkdirSync(path.dirname(venv), { recursive: true });
    const created = run(python, ["-m", "venv", venv]);
    if (created.status !== 0) {
      return created;
    }
  }

  if (forceInstall || !fs.existsSync(executable) || readMarker(marker) !== source) {
    console.error("正在准备 cc-harness Python 运行环境，首次安装可能需要一些时间…");
    const installed = run(pythonPath, [
      "-m",
      "pip",
      "install",
      "--upgrade",
      source,
    ]);
    if (installed.status !== 0) {
      return installed;
    }
    writeMarker(marker, source);
  }

  return run(executable, args);
}

function main() {
  const args = process.argv.slice(2);
  if (commandExists("uv")) {
    const result = runWithUv(args);
    const status = exitFrom(result);
    if (status !== null) {
      process.exitCode = status;
      return;
    }
  }

  const result = runWithPython(args);
  process.exitCode = result.status ?? 1;
}

main();
