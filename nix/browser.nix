{ runCommand, fetchurl, bun, gnutar, patch }:
let
  manifest = builtins.fromJSON (builtins.readFile ../package.json);
  version = manifest.dependencies.single-file-core;
  source = fetchurl {
    url = "https://registry.npmjs.org/single-file-core/-/single-file-core-${version}.tgz";
    hash = "sha512-e49IxLMaChJYha9uVyhxJNVL7ABoGHGm+U2fhnVWBZhzqMto5pP0X9QcLmqF8b46+ixvHFkvB8VPTECEw2vVWA==";
  };
in
runCommand "page-archiver-browser" { nativeBuildInputs = [ bun gnutar patch ]; } ''
  mkdir -p browser scripts node_modules/single-file-core src/page_archiver/assets
  cp ${../browser/capture.js} browser/capture.js
  cp ${../scripts/build-browser.ts} scripts/build-browser.ts
  tar xf ${source} --strip-components=1 -C node_modules/single-file-core
  patch -d node_modules/single-file-core -p1 < ${../patches + "/single-file-core-1.6.22.patch"}
  bun run scripts/build-browser.ts
  mkdir -p $out
  cp src/page_archiver/assets/* $out/
''
