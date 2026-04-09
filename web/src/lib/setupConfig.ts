const MASKED_SECRET = '***MASKED***';

export type AccessMode = 'existing' | 'deepseek' | 'codex';

export interface SetupConfigDraft {
  accessMode: AccessMode;
  provider: string;
  model: string;
  apiKey: string;
  hasExistingApiKey: boolean;
  telegramEnabled: boolean;
  telegramBotToken: string;
  hasExistingTelegramToken: boolean;
  telegramAllowedUsers: string;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function findTopLevelValue(text: string, key: string): string | null {
  const match = text.match(new RegExp(`^${escapeRegExp(key)}\\s*=\\s*"(.*)"\\s*$`, 'm'));
  return match && typeof match[1] === 'string' ? match[1] : null;
}

function parseTomlStringArray(raw: string | null): string[] {
  if (!raw) return [];
  const match = raw.match(/\[(.*)\]/);
  const inner = match && typeof match[1] === 'string' ? match[1] : null;
  if (!inner) return [];
  return inner
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => item.replace(/^"/, '').replace(/"$/, ''));
}

function findSectionRange(lines: string[], sectionName: string): { start: number; end: number } | null {
  const header = `[${sectionName}]`;
  const start = lines.findIndex((line) => line.trim() === header);
  if (start === -1) return null;

  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    if (lines[index]?.trim().startsWith('[')) {
      end = index;
      break;
    }
  }

  return { start, end };
}

function ensureSection(lines: string[], sectionName: string): { start: number; end: number } {
  const existing = findSectionRange(lines, sectionName);
  if (existing) return existing;

  if (lines.length > 0 && lines[lines.length - 1]?.trim() !== '') {
    lines.push('');
  }
  lines.push(`[${sectionName}]`);
  return { start: lines.length - 1, end: lines.length };
}

function upsertLine(
  lines: string[],
  key: string,
  renderedValue: string,
  sectionName?: string,
): string[] {
  const scope = sectionName ? ensureSection(lines, sectionName) : {
    start: -1,
    end: lines.findIndex((line) => line.trim().startsWith('[')),
  };
  const start = scope.start + 1;
  const end = scope.end === -1 ? lines.length : scope.end;
  const keyPrefix = `${key} = `;

  for (let index = start; index < end; index += 1) {
    if (lines[index]?.trim().startsWith(keyPrefix)) {
      lines[index] = `${key} = ${renderedValue}`;
      return lines;
    }
  }

  lines.splice(end, 0, `${key} = ${renderedValue}`);
  return lines;
}

function setTomlString(
  lines: string[],
  key: string,
  value: string,
  sectionName?: string,
): string[] {
  return upsertLine(lines, key, JSON.stringify(value), sectionName);
}

function setTomlStringArray(
  lines: string[],
  key: string,
  values: string[],
  sectionName?: string,
): string[] {
  const rendered = `[${values.map((value) => JSON.stringify(value)).join(', ')}]`;
  return upsertLine(lines, key, rendered, sectionName);
}

function readSectionString(text: string, sectionName: string, key: string): string | null {
  const range = findSectionRange(text.split('\n'), sectionName);
  if (!range) return null;
  const section = text.split('\n').slice(range.start, range.end).join('\n');
  const match = section.match(new RegExp(`^${escapeRegExp(key)}\\s*=\\s*"(.*)"\\s*$`, 'm'));
  return match?.[1] ?? null;
}

function readSectionArray(text: string, sectionName: string, key: string): string[] {
  const range = findSectionRange(text.split('\n'), sectionName);
  if (!range) return [];
  const section = text.split('\n').slice(range.start, range.end).join('\n');
  const match = section.match(new RegExp(`^${escapeRegExp(key)}\\s*=\\s*(\\[.*\\])\\s*$`, 'm'));
  return parseTomlStringArray(match?.[1] ?? null);
}

export function parseSetupConfig(text: string): SetupConfigDraft {
  const provider = findTopLevelValue(text, 'default_provider') ?? '';
  const model = findTopLevelValue(text, 'default_model') ?? '';
  const apiKey = findTopLevelValue(text, 'api_key') ?? '';
  const telegramBotToken = readSectionString(text, 'channels_config.telegram', 'bot_token') ?? '';
  const telegramAllowedUsers = readSectionArray(text, 'channels_config.telegram', 'allowed_users');

  const accessMode: AccessMode =
    provider === 'deepseek'
      ? 'deepseek'
      : provider === 'openai-codex' || provider === 'codex'
        ? 'codex'
        : 'existing';

  return {
    accessMode,
    provider,
    model,
    apiKey: apiKey === MASKED_SECRET ? '' : apiKey,
    hasExistingApiKey: apiKey === MASKED_SECRET || apiKey.length > 0,
    telegramEnabled: telegramBotToken.length > 0,
    telegramBotToken: telegramBotToken === MASKED_SECRET ? '' : telegramBotToken,
    hasExistingTelegramToken: telegramBotToken === MASKED_SECRET || telegramBotToken.length > 0,
    telegramAllowedUsers: telegramAllowedUsers.join(', '),
  };
}

export function applySetupConfig(text: string, draft: SetupConfigDraft): string {
  const lines = text.split('\n');

  if (draft.accessMode === 'deepseek') {
    setTomlString(lines, 'default_provider', 'deepseek');
    setTomlString(lines, 'default_model', draft.model || 'deepseek-chat');
    if (draft.apiKey.trim()) {
      setTomlString(lines, 'api_key', draft.apiKey.trim());
    } else if (!draft.hasExistingApiKey) {
      setTomlString(lines, 'api_key', '');
    }
  } else if (draft.accessMode === 'codex') {
    setTomlString(lines, 'default_provider', 'openai-codex');
    setTomlString(lines, 'default_model', draft.model || 'gpt-5-codex');
  }

  if (draft.telegramEnabled) {
    if (draft.telegramBotToken.trim()) {
      setTomlString(lines, 'bot_token', draft.telegramBotToken.trim(), 'channels_config.telegram');
    }
    const users = draft.telegramAllowedUsers
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean);
    setTomlStringArray(lines, 'allowed_users', users, 'channels_config.telegram');
  }

  return `${lines.join('\n').replace(/\n{3,}/g, '\n\n')}\n`;
}
