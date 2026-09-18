/**
 * Emit the example topology as a static asset for the backend-free demo build.
 *
 * Converts the object form the Python services use into the same compact
 * columnar shape `GET /api/topology?format=compact` returns, so the demo and
 * the live app parse identical bytes through identical code. About 4x smaller
 * than the object form, which matters when it ships in a static bundle.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(here, "../../data/example-topology.json");
const target = path.resolve(here, "../public/demo-topology.json");

if (!fs.existsSync(source)) {
  console.error(
    `[demo-data] no topology at ${source}\n` +
    `            generate one first:  python -m discovery.synthesize --nodes 400`
  );
  process.exit(1);
}

const topology = JSON.parse(fs.readFileSync(source, "utf8"));
const index = new Map(topology.nodes.map((node, i) => [node.id, i]));

const NODE_FIELDS = ["id", "name", "ip", "mac", "vendor", "device_type", "os",
                     "subnet", "site", "criticality", "layer", "x", "y", "z", "tags"];

const round = (value) => Math.round(value * 1000) / 1000;

const nodes = topology.nodes.map((n) => [
  n.id, n.name, n.ip || "", n.mac || "", n.vendor, n.device_type, n.os,
  n.subnet, n.site, n.criticality, n.layer,
  round(n.position.x), round(n.position.y), round(n.position.z), n.tags || [],
]);

const links = topology.links
  .filter((l) => index.has(l.source) && index.has(l.target))
  .map((l) => [index.get(l.source), index.get(l.target), l.kind]);

const xs = topology.nodes.map((n) => n.position.x);
const ys = topology.nodes.map((n) => n.position.y);
const zs = topology.nodes.map((n) => n.position.z);
const span = (a) => [Math.min(...a), Math.max(...a)];
const [minX, maxX] = span(xs);
const [minY, maxY] = span(ys);
const [minZ, maxZ] = span(zs);

const byType = {};
const bySubnet = {};
for (const node of topology.nodes) {
  byType[node.device_type] = (byType[node.device_type] || 0) + 1;
  bySubnet[node.subnet] = (bySubnet[node.subnet] || 0) + 1;
}

const compact = {
  format: "compact",
  version: topology.version,
  generated_at: topology.generated_at,
  source: topology.source,
  node_fields: NODE_FIELDS,
  link_fields: ["source", "target", "kind"],
  nodes,
  links,
  bounds: {
    min: { x: minX, y: minY, z: minZ },
    max: { x: maxX, y: maxY, z: maxZ },
    center: { x: (minX + maxX) / 2, y: (minY + maxY) / 2, z: (minZ + maxZ) / 2 },
    radius: Math.max(maxX - minX, maxY - minY, maxZ - minZ) / 2 || 1,
  },
  stats: {
    nodes: topology.nodes.length,
    links: links.length,
    by_type: byType,
    by_subnet: bySubnet,
    vendors: [...new Set(topology.nodes.map((n) => n.vendor))].sort(),
  },
};

fs.mkdirSync(path.dirname(target), { recursive: true });
fs.writeFileSync(target, JSON.stringify(compact));

const kb = (n) => `${(n / 1024).toFixed(0)} kB`;
console.log(
  `[demo-data] ${compact.nodes.length} nodes, ${compact.links.length} links -> ` +
  `public/demo-topology.json (${kb(fs.statSync(target).size)}, ` +
  `from ${kb(fs.statSync(source).size)})`
);
