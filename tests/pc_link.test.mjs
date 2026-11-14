import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { EventEmitter } from 'node:events';

test('PC Bot keeps account commands, removes duplicates and completes a private linking flow', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'pc-link-test-'));
  const previousCwd = process.cwd();
  const originalInterval = globalThis.setInterval;
  const originalTimeout = globalThis.setTimeout;
  const realNow = Date.now;
  try {
    process.chdir(directory);
    globalThis.setInterval = () => ({ unref() {} });
    globalThis.setTimeout = () => ({ unref() {} });
    const { setupPCLink, preparePCCommandRegistration } = await import('../PC-Bridge/scripts/linkSetup.js');
    const hooks = [], commands = [], messages = [], errors = [];
    const player = { name: 'TestPlayer', sendMessage: async text => messages.push(text) };
    const deleted = [];
    const makeCommand = (name, scope) => ({ name, async delete() {
      deleted.push(`${scope}:${name}`);
      const list = scope === 'guild' ? guildCommands : globalCommands;
      list.splice(list.indexOf(this), 1);
    } });
    const guildCommands = ['player', 'vs', 'wordwolfex', '8192game', 'list', 'unrelated'].map(name => makeCommand(name, 'guild'));
    const globalCommands = ['link', 'delete', 'player', 'vs', 'wordwolfex', '8192game', 'list', 'unrelated-global'].map(name => makeCommand(name, 'global'));
    const guild = { commands: {
      create: async data => {
        commands.push(data);
        if (!guildCommands.some(command => command.name === data.name)) guildCommands.push(makeCommand(data.name, 'guild'));
      },
      fetch: async () => new Map(guildCommands.map((command, index) => [index, command])),
    } };
    const client = new EventEmitter();
    client.guilds = { cache: new Map([['123', guild]]) };
    client.application = { commands: { fetch: async () => new Map(globalCommands.map((command, index) => [index, command])) } };
    const properties = new Map();
    const app = {
      env: { GUILD_ID: '123' },
      properties,
      before: (event, handler) => hooks.push(handler),
      bot: { client, awaitReady: async () => {} },
      minecraft: { getWorlds: () => [] },
      logger: { info() {}, debug() {}, warn() {}, error: (...args) => errors.push(args) },
    };
    const events = new EventEmitter();
    app.once = events.once.bind(events);
    preparePCCommandRegistration(app);
    const starting = setupPCLink(app);
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(commands.length, 0, 'Wait for the slow Bridge bulk registration before creating custom commands');
    // Model the library replacing guild commands after the client is ready.
    // The custom commands must be added only AFTER this operation.
    guildCommands.push(makeCommand('ping', 'guild'));
    events.emit('discordReady');
    await starting;
    assert.deepEqual(commands.map(command => command.name), ['link', 'delete']);
    for (const name of ['player', 'vs', 'wordwolfex', '8192game']) assert.ok(deleted.includes(`guild:${name}`));
    for (const name of ['link', 'delete', 'player', 'vs', 'wordwolfex', '8192game', 'list']) assert.ok(deleted.includes(`global:${name}`));
    assert.ok(!deleted.includes('guild:list'));
    assert.ok(!deleted.includes('guild:unrelated'));
    assert.ok(!deleted.includes('global:unrelated-global'));
    for (const name of ['list', 'ping', 'link', 'delete']) assert.ok(guildCommands.some(command => command.name === name));
    assert.ok(!guildCommands.some(command => ['player', 'vs', 'wordwolfex', '8192game'].includes(command.name)));
    assert.equal(hooks[0]({ message: 'ordinary chat', sender: player }), true);
    assert.equal(hooks[0]({ message: '!link', sender: player }), false);
    const code = messages[0].match(/\b\d{8}\b/)[0];
    assert.equal(hooks[0]({ message: '!link', sender: player }), false);
    assert.equal(messages.length, 2); // Repeated requests produce a cooldown response.

    const makeInteraction = (id, value = code, guildId = '123') => ({
      id, guildId, commandName: 'link', isChatInputCommand: () => true,
      options: { getString: () => value },
      user: { id: '123456789012345678', username: 'TestDiscord', tag: 'TestDiscord' },
      replies: [],
      async deferReply(options) { this.deferred = true; this.deferOptions = options; },
      async editReply(response) { this.replies.push(response); },
    });
    const listener = client.listeners('interactionCreate')[0];
    const wrongGuild = makeInteraction('outside', code, '456');
    await listener(wrongGuild);
    assert.equal(wrongGuild.deferred, undefined);
    const valid = makeInteraction('valid');
    await listener(valid);
    assert.equal(valid.deferOptions.flags, 64);
    const accountPath = join(directory, '.discord-mcbe', 'account.json');
    const saved = readFileSync(accountPath, 'utf8');
    assert.deepEqual(JSON.parse(saved), [{
      mcid: 'TestPlayer', 'discord-name': 'TestDiscord', 'discord-id': '123456789012345678',
    }]);
    await listener(valid); // Duplicate gateway delivery is ignored.
    assert.equal(valid.replies.length, 1);
    const reused = makeInteraction('reused');
    await listener(reused);
    assert.equal(reused.replies[0].embeds[0].color, 0xd32f2f);
    assert.equal(readFileSync(accountPath, 'utf8'), saved);
    const deniedDelete = makeInteraction('delete-denied');
    deniedDelete.commandName = 'delete';
    await listener(deniedDelete);
    assert.equal(deniedDelete.deferOptions.flags, 64);
    assert.equal(readFileSync(accountPath, 'utf8'), saved);

    Date.now = () => realNow() + 6 * 60 * 1000;
    hooks[0]({ message: '!link', sender: player });
    const expiredCode = messages.at(-1).match(/\b\d{8}\b/)[0];
    Date.now = () => realNow() + 12 * 60 * 1000;
    const expired = makeInteraction('expired', expiredCode);
    await listener(expired);
    assert.equal(expired.replies[0].embeds[0].color, 0xd32f2f);
    assert.equal(readFileSync(accountPath, 'utf8'), saved);
    await setupPCLink(app);
    assert.equal(client.listenerCount('interactionCreate'), 1);
    assert.equal(hooks.length, 1);
    assert.deepEqual(errors, []);
  } finally {
    Date.now = realNow;
    globalThis.setInterval = originalInterval;
    globalThis.setTimeout = originalTimeout;
    process.chdir(previousCwd);
    rmSync(directory, { recursive: true, force: true });
  }
});
