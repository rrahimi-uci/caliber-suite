#!/usr/bin/env node
// Cross-platform preflight for the `allure:*` npm scripts: the Allure report
// generator is a Java app, so fail early with a clear, OS-specific hint instead
// of a cryptic stack trace when no JRE is on PATH. Node is always present here
// (it's an npm project), so this runs on macOS / Linux / Windows alike.
import { spawnSync } from "node:child_process";

const probe = spawnSync("java", ["-version"], { stdio: "ignore" });
if (probe.status === 0) process.exit(0);

process.stderr.write(
  [
    "",
    "Allure report generation needs a Java runtime, which wasn't found on PATH.",
    "",
    "Install one:",
    "  macOS:          brew install --cask temurin      (or: brew install openjdk)",
    "  Debian/Ubuntu:  sudo apt-get install -y default-jre",
    "  Fedora/RHEL:    sudo dnf install -y java-21-openjdk-headless",
    "  Windows:        choco install temurin            (or: scoop install temurin)",
    "",
    "Or render without installing Java (uses Docker) from the suite root:",
    "  make allure",
    "",
  ].join("\n"),
);
process.exit(1);
