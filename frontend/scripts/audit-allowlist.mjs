#!/usr/bin/env node
// Production npm-audit gate with an explicit advisory allowlist.
//
// Mirrors the backend's `pip-audit --ignore-vuln <id>` pattern: fails CI on any
// high/critical advisory that is NOT explicitly allowlisted below (with a
// reason). A plain `npm audit --audit-level=high` can't ignore a single
// unreachable advisory, which is why this wrapper exists.
//
// Run: node scripts/audit-allowlist.mjs   (from frontend/)

import { execSync } from "node:child_process";

// GHSA id -> reason. Only add an entry when the advisory is genuinely
// unreachable in this app; keep the reason specific.
const ALLOWLIST = {
  "GHSA-qwww-vcr4-c8h2":
    "React Router RSC-mode CSRF bypass. This is a client-only SPA that does " +
    "not use React Server Components / RSC mode, so the vulnerable code path " +
    "is never reached. The only 'fix' is a breaking downgrade to " +
    "react-router-dom@7.11.0. Revisit if we adopt RSC or react-router ships a " +
    "forward fix.",
};

const GATE = new Set(["high", "critical"]);

function runAudit() {
  try {
    // Exits 0 when clean.
    return execSync("npm audit --omit=dev --json", { encoding: "utf8" });
  } catch (err) {
    // npm audit exits non-zero when vulnerabilities are found; the JSON report
    // is still on stdout.
    if (err.stdout) return err.stdout;
    throw err;
  }
}

const report = JSON.parse(runAudit());
const vulns = report.vulnerabilities ?? {};

// Collect every distinct high/critical GHSA advisory in the report.
const found = new Map(); // ghsa -> { severity, title }
for (const info of Object.values(vulns)) {
  if (!GATE.has(info.severity)) continue;
  for (const via of info.via ?? []) {
    if (typeof via !== "object" || !via.url) continue;
    const ghsa = via.url.split("/").pop();
    if (GATE.has((via.severity ?? info.severity))) {
      found.set(ghsa, { severity: via.severity ?? info.severity, title: via.title });
    }
  }
}

const unhandled = [...found.entries()].filter(([ghsa]) => !(ghsa in ALLOWLIST));

if (unhandled.length === 0) {
  const allowed = [...found.keys()].filter((g) => g in ALLOWLIST);
  if (allowed.length) {
    console.log(`npm audit: no unhandled high/critical advisories.`);
    console.log(`Allowlisted (see scripts/audit-allowlist.mjs):`);
    for (const g of allowed) console.log(`  - ${g}: ${found.get(g).title}`);
  } else {
    console.log("npm audit: no high/critical advisories.");
  }
  process.exit(0);
}

console.error("npm audit: unhandled high/critical advisories:\n");
for (const [ghsa, meta] of unhandled) {
  console.error(`  ${meta.severity.toUpperCase()}  ${ghsa}  ${meta.title ?? ""}`);
}
console.error(
  "\nFix the dependency, or (only if genuinely unreachable) add the GHSA id to " +
    "ALLOWLIST in scripts/audit-allowlist.mjs with a specific reason.",
);
process.exit(1);
