// Run the enreal wheel under Pyodide (Node) the same way web/worker.js does in a browser.
//
//   node parity.mjs <wheel> <cases.json>
//
// cases.json: [{ "image": "<path>", "params": { ... } }, ...]
// Prints one JSON array to stdout: [{ "ok": true, "json": "<StageResult JSON>" } | { "ok": false, "error": "..." }]
//
// The Python glue is read out of web/worker.js rather than copied here, so this test always
// exercises the exact code the browser runs.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide, version as pyodideVersion } from "pyodide";

const here = dirname(fileURLToPath(import.meta.url));
const [wheelPath, casesPath] = process.argv.slice(2);
if (!wheelPath || !casesPath) {
  console.error("usage: node parity.mjs <wheel> <cases.json>");
  process.exit(2);
}

const workerSource = readFileSync(resolve(here, "../../web/worker.js"), "utf8");
const glue = workerSource.match(/runPython\(`([\s\S]*?def _run[\s\S]*?)`\)/);
if (!glue) {
  console.error("Could not find the _run glue in web/worker.js");
  process.exit(2);
}
const packagesMatch = readFileSync(resolve(here, "../../scripts/build_web.py"), "utf8").match(/PYODIDE_PACKAGES = (\[[^\]]*\])/);
const packages = JSON.parse(packagesMatch[1].replace(/'/g, '"'));

const pyodide = await loadPyodide({
  // In Node the runtime comes from node_modules, and packages are downloaded from the
  // matching jsDelivr release on first use and cached next to it.
  stdout: (line) => console.error(line),
  stderr: (line) => console.error(line),
});
await pyodide.loadPackage(packages, { messageCallback: () => {} });

const sitePackages = pyodide.runPython("import sysconfig; sysconfig.get_paths()['purelib']");
// Pyodide rejects Node's Buffer subclass, so hand it a plain Uint8Array.
pyodide.unpackArchive(new Uint8Array(readFileSync(wheelPath)), "wheel", { extractDir: sitePackages });
const run = pyodide.runPython(glue[1]);

const cases = JSON.parse(readFileSync(casesPath, "utf8"));
const results = [];
for (const c of cases) {
  try {
    const bytes = new Uint8Array(readFileSync(c.image));
    results.push({ ok: true, json: run(bytes, JSON.stringify(c.params)) });
  } catch (err) {
    const lines = String(err.message || err).trim().split("\n");
    results.push({ ok: false, error: lines[lines.length - 1] });
  }
}
const cv2 = pyodide.runPython("import cv2; cv2.__version__");
console.error(`pyodide ${pyodideVersion}, cv2 ${cv2}`);
process.stdout.write(JSON.stringify(results));
