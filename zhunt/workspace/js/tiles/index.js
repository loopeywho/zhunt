import { chatTile } from './chat.js';
import { compareTile } from './compare.js';
import { routingTile } from './routing.js';
import { providersTile } from './providers.js';
import { logsTile } from './logs.js';

const FACTORIES = { chat: chatTile, compare: compareTile, routing: routingTile, providers: providersTile, logs: logsTile };

export const KIND_LABELS = {
  chat: 'Chat', compare: 'Compare', routing: 'Routing (why this model)', providers: 'Providers (keys and quota)', logs: 'Logs',
};

// services: {store, toast}. Returns the factory the tiling manager calls.
export function createTileFactory(services) {
  return (kind, state, ctx) => FACTORIES[kind](services, state || {}, ctx);
}
