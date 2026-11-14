import { Application, renderLogo } from '@discord-mcbe/server';
import { preparePCCommandRegistration } from './scripts/linkSetup.js';
renderLogo();
const app = new Application();
preparePCCommandRegistration(app);
await app.start();
