import { chatTile } from './chat.js';
import { compareTile } from './compare.js';
import { routingTile } from './routing.js';
import { providersTile } from './providers.js';
import { logsTile } from './logs.js';
import { foryouTile } from './foryou.js';
import { panelsTile } from './panels.js';

const FACTORIES = { chat: chatTile, foryou: foryouTile, panels: panelsTile, compare: compareTile, routing: routingTile, providers: providersTile, logs: logsTile };

export const KIND_LABELS = {
  chat: 'Chat', foryou: 'For you', panels: 'Panels', compare: 'Compare', routing: 'Routing (why this model)', providers: 'Providers (keys and quota)', logs: 'Logs',
};

// services: {store, toast, openWindow, openPlan}. Returns the factory the tiling manager calls.
export function createTileFactory(services) {
  return (kind, state, ctx) => FACTORIES[kind](services, state || {}, ctx);
}
