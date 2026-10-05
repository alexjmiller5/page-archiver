const result = await Bun.build({
  entrypoints: ['browser/capture.js'],
  outdir: 'src/page_archiver/assets',
  naming: 'capture.js',
  target: 'browser',
  format: 'iife',
  minify: true,
  // Compression workers are unused for plain HTML; the classic injected
  // script cannot contain the optional worker's module-only URL expression.
  define: { 'import.meta.url': '""' },
});
if (!result.success) {
  for (const log of result.logs) console.error(log);
  process.exit(1);
}

// Preserve upstream notices even when minification removes source comments.
const notices = new Set<string>();
const dependency = 'node_modules/single-file-core';
const files = Array.from(new Bun.Glob('**/*.js').scanSync(dependency)).sort();
for (const file of files) {
  const source = await Bun.file(`${dependency}/${file}`).text();
  for (const [comment] of source.matchAll(/\/\*[\s\S]*?\*\//g)) {
    if (/copyright|\blicen[cs]e\b/i.test(comment)) notices.add(comment);
  }
}
await Bun.write('src/page_archiver/assets/THIRD_PARTY_NOTICES.txt',
  'SingleFile Core 1.6.22: https://github.com/gildas-lormeau/single-file-core\n' +
  'Modified by the bundled patch for DOM form-field shadowing, raw-text nesting markers, script-free nesting repair, and excluded-script normalization.\n\n' +
  [...notices].join('\n\n'));
