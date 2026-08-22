# Vendored D3

`d3.v7.min.js` is vendored locally so the graph — the product's centerpiece —
renders even on a network that blocks public CDNs. `frontend/index.html`
previously loaded D3 from `https://d3js.org/d3.v7.min.js`; that host (along
with `cdn.jsdelivr.net`, `unpkg.com`, `cdnjs.cloudflare.com`) is unreachable
from some networks (e.g. this sandbox's proxy returns 403 for all of them),
which threw inside `renderGraph()` and dropped the UI to its error empty
state. Serving the file from `/vendor/d3.v7.min.js` (mounted by
`src/main.py`) removes that live-demo failure mode: venue wifi blocking a CDN
must not kill the visualization.

## Provenance

- **Upstream**: https://github.com/d3/d3
- **Version**: 7.9.0
- **Source**: `https://registry.npmjs.org/d3/-/d3-7.9.0.tgz` (npm registry —
  the only source reachable through the proxy in this environment;
  `d3js.org`/jsdelivr/unpkg/cdnjs are all blocked)
- **File**: `package/dist/d3.min.js` extracted from that tarball, unmodified
- **License**: ISC (see https://github.com/d3/d3/blob/main/LICENSE)

### Checksums (sha256)

- `d3-7.9.0.tgz` (the npm tarball as downloaded):
  `7e36605710a2ba54846797c8c6d888911341b215ae53257dc32a49a0b824355e`
- `d3.v7.min.js` (the extracted, vendored file):
  `f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539`

## Re-vendoring

To update or re-verify this file:

```bash
curl -sSL https://registry.npmjs.org/d3/-/d3-7.9.0.tgz -o /tmp/d3.tgz
sha256sum /tmp/d3.tgz
tar -xzOf /tmp/d3.tgz package/dist/d3.min.js > frontend/vendor/d3.v7.min.js
sha256sum frontend/vendor/d3.v7.min.js
```

Update the checksums above (and the version, if it changed) after re-running.
`tests/test_vendor_assets.py` parses the `d3.v7.min.js` checksum out of this
README and fails if it drifts from the file on disk, so the two can never go
out of sync silently.
