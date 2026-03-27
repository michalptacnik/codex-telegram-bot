import fs from 'node:fs';
import path from 'node:path';

const bundleDir = path.resolve(process.env.BUNDLE_DIR ?? 'src-tauri/target/release/bundle');
const repository = process.env.GITHUB_REPOSITORY;
const refName = process.env.GITHUB_REF_NAME;
const version = (process.env.RELEASE_VERSION ?? '').trim() || refName?.replace(/^v/, '') || '';

if (!repository) {
  throw new Error('Missing GITHUB_REPOSITORY.');
}

if (!refName) {
  throw new Error('Missing GITHUB_REF_NAME.');
}

if (!version) {
  throw new Error('Missing RELEASE_VERSION.');
}

const macosDir = path.join(bundleDir, 'macos');
const archives = fs
  .readdirSync(macosDir)
  .filter((entry) => entry.endsWith('.app.tar.gz'))
  .sort();

if (archives.length === 0) {
  throw new Error(`No macOS updater archive found in ${macosDir}.`);
}

const archiveName = archives[0];
const signaturePath = path.join(macosDir, `${archiveName}.sig`);

if (!fs.existsSync(signaturePath)) {
  throw new Error(`Missing updater signature for ${archiveName}.`);
}

const signature = fs.readFileSync(signaturePath, 'utf8').trim();
const encodedArchiveName = archiveName.replace(/ /g, '%20');
const manifest = {
  version,
  notes: `Agent HQ desktop release ${version}.`,
  pub_date: new Date().toISOString(),
  platforms: {
    'darwin-aarch64': {
      signature,
      url: `https://github.com/${repository}/releases/download/${refName}/${encodedArchiveName}`,
    },
  },
};

const outputPath = path.join(bundleDir, 'latest.json');
fs.writeFileSync(outputPath, `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Wrote updater manifest to ${outputPath}`);
