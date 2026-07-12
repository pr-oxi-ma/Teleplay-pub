import { chmodSync, writeFileSync } from "node:fs";

const [outputPath, requiredCsv = ""] = process.argv.slice(2);
const allNames = [
  "EDGE_SIGNING_SECRET",
  "ORIGIN_SECRET",
  "TOUCH_SECRET",
];

if (!outputPath) {
  console.error("Usage: node write-bootstrap-secrets.mjs <output-file> <required-csv>");
  process.exit(2);
}

const requiredNames = new Set(
  (requiredCsv || allNames.join(","))
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean),
);

const unknownNames = [...requiredNames].filter(
  (name) => !allNames.includes(name),
);
if (unknownNames.length > 0) {
  console.error(`Unknown Worker secret names: ${unknownNames.join(", ")}`);
  process.exit(2);
}

const missingValues = [...requiredNames].filter((name) => !process.env[name]);
if (missingValues.length > 0) {
  console.error(
    `::error::First deployment or missing Worker bindings require these GitHub Actions secrets: ${missingValues.join(", ")}`,
  );
  process.exit(1);
}

// Upload only the bindings detected as missing. Existing deployed secrets are
// omitted from this file and therefore remain untouched by Wrangler.
const values = Object.fromEntries(
  [...requiredNames].map((name) => [name, process.env[name]]),
);

writeFileSync(outputPath, JSON.stringify(values), {
  encoding: "utf8",
  mode: 0o600,
});
chmodSync(outputPath, 0o600);
