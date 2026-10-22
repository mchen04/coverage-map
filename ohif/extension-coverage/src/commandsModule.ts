import { PRESETS } from './presets';
import type CoverageService from './CoverageService';

/** Coverage-mode commands; the mode binds them to N D A G O F 1-5. */
export default function getCommandsModule({ servicesManager }) {
  const svc = (): CoverageService => servicesManager.services.coverageService;

  const actions = {
    covmapSetWindow: ({ preset }: { preset: string }) => {
      const p = PRESETS[preset];
      if (p) {
        svc().applyPreset(p);
      }
    },
    covmapNextGap: () => svc().nextGap(),
    covmapMarkDone: () => svc().toggleDone(),
    covmapMarkNA: () => svc().toggleNA(),
    covmapFlag: () => svc().toggleFlag(),
    covmapToggleOutline: () => svc().toggleOutline(),
    covmapFinish: () => svc().openFinish(),
    covmapJumpToOrgan: ({ key }: { key: string }) => svc().jumpToOrgan(key),
  };

  const definitions = Object.fromEntries(
    Object.entries(actions).map(([name, commandFn]) => [name, { commandFn }])
  );

  return { actions, definitions, defaultContext: 'COVMAP' };
}
