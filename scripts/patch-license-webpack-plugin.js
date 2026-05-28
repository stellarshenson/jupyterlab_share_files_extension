/**
 * Patch license-webpack-plugin to tolerate webpack 5's "provide module"
 * identifiers that have no `=` token.
 *
 * Upstream bug: WebpackInnerModuleIterator.js calls filename.split('=')[1].trim()
 * unconditionally. webpack 5 emits identifiers like
 *   "provide shared module (default) foo@1.0 ..."
 * without an `=`, so `[1]` is undefined and .trim() throws.
 *
 * Safe to run multiple times - re-runs are no-ops because the patched line is
 * idempotent.
 */
const fs = require('fs');
const path = require('path');

const candidates = [
  path.join(
    'node_modules',
    'license-webpack-plugin',
    'dist',
    'WebpackModuleFileIterator.js'
  ),
  path.join(
    'node_modules',
    'license-webpack-plugin',
    'dist',
    'WebpackInnerModuleIterator.js'
  )
];

const BAD = "return filename.split('=')[1].trim();";
const GOOD = "var _p = filename.split('=')[1]; return _p ? _p.trim() : null;";

for (const file of candidates) {
  if (!fs.existsSync(file)) continue;
  const src = fs.readFileSync(file, 'utf8');
  if (!src.includes(BAD)) continue;
  fs.writeFileSync(file, src.replace(BAD, GOOD));
  console.log('Patched ' + file);
}
