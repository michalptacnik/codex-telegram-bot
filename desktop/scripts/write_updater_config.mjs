import fs from 'node:fs';
import path from 'node:path';

function resolveEndpoints() {
  const explicit = (process.env.AGENT_HQ_UPDATER_ENDPOINTS ?? '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);

  if (explicit.length > 0) {
    return explicit;
  }

  const repository = process.env.GITHUB_REPOSITORY;
  if (!repository) {
    throw new Error(
      'Missing AGENT_HQ_UPDATER_ENDPOINTS. Set it explicitly or provide GITHUB_REPOSITORY for the default GitHub Releases feed.',
    );
  }

  return [`https://github.com/${repository}/releases/latest/download/latest.json`];
}

const pubkey = (process.env.AGENT_HQ_UPDATER_PUBKEY ?? '').trim();
if (!pubkey) {
  throw new Error('Missing AGENT_HQ_UPDATER_PUBKEY.');
}

const config = {
  bundle: {
    createUpdaterArtifacts: true,
  },
  plugins: {
    updater: {
      pubkey,
      endpoints: resolveEndpoints(),
    },
  },
};

const outputPath = path.resolve('src-tauri/tauri.generated.updater.conf.json');
fs.writeFileSync(outputPath, `${JSON.stringify(config, null, 2)}\n`);
console.log(`Wrote updater config to ${outputPath}`);
