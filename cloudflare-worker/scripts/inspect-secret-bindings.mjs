import { readFileSync } from "node:fs";

const [filePath, ...requiredNames] = process.argv.slice(2);

if (!filePath || requiredNames.length === 0) {
  console.error("Usage: node inspect-secret-bindings.mjs <json-file> <secret>...");
  process.exit(2);
}

let listed;
try {
  listed = JSON.parse(readFileSync(filePath, "utf8"));
} catch (error) {
  console.error(`Unable to parse Wrangler secret list: ${error.message}`);
  process.exit(2);
}

if (!Array.isArray(listed)) {
  console.error("Wrangler secret list did not return a JSON array.");
  process.exit(2);
}

const existing = new Set(
  listed
    .map((item) => item?.name)
    .filter((name) => typeof name === "string" && name.length > 0),
);

process.stdout.write(
  requiredNames.filter((name) => !existing.has(name)).join(","),
);
