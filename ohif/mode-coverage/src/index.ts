import { id } from './id';
import {
  basicLayout,
  basicRoute,
  extensionDependencies as basicDependencies,
  mode as basicMode,
  modeInstance as basicModeInstance,
  ohif,
} from '@ohif/mode-basic';

/**
 * Coverage Map mode (research use only): the basic viewer with the coverage panel on the
 * right. It starts the reading instrumentation on enter and replaces the hotkeys that
 * collide with its own (N D A G O F 1-5); every other mode keeps the stock bindings.
 */
const coverageExtension = '@covmap/extension-coverage';
const coveragePanel = `${coverageExtension}.panelModule.coverage`;

export const extensionDependencies = {
  ...basicDependencies,
  [coverageExtension]: '^0.1.0',
};

const HOTKEYS = [
  { commandName: 'covmapNextGap', label: 'Coverage: next gap', keys: ['n'] },
  { commandName: 'covmapMarkDone', label: 'Coverage: mark selected organ done', keys: ['d'] },
  { commandName: 'covmapMarkNA', label: 'Coverage: mark selected organ N/A', keys: ['a'] },
  { commandName: 'covmapFlag', label: 'Coverage: flag selected organ segmentation', keys: ['g'] },
  { commandName: 'covmapToggleOutline', label: 'Coverage: outline selected organ', keys: ['o'] },
  { commandName: 'covmapFinish', label: 'Coverage: finish', keys: ['f'] },
  ...[
    ['soft', '1', 'Soft tissue 400/40'],
    ['lung', '2', 'Lung 1500/-600'],
    ['bone', '3', 'Bone 2500/480'],
    ['liver', '4', 'Liver 150/90'],
    ['brain', '5', 'Brain 80/40'],
  ].map(([preset, key, label]) => ({
    commandName: 'covmapSetWindow',
    commandOptions: { preset },
    label: `Window: ${label}`,
    keys: [key],
  })),
];

const OUR_KEYS = new Set(HOTKEYS.flatMap(h => h.keys));

function onModeEnter(args) {
  basicModeInstance.onModeEnter.call(this, args);
  const { servicesManager, appConfig } = args;
  const { customizationService, coverageService } = servicesManager.services;

  const stock = customizationService.getCustomization('ohif.hotkeyBindings') ?? [];
  const kept = stock.filter(b => !(b.keys ?? []).some(k => OUR_KEYS.has(String(k).toLowerCase())));
  customizationService.setCustomizations({
    'ohif.hotkeyBindings': { $set: [...kept, ...HOTKEYS] },
  });

  coverageService.start({ appConfig });
  // one-page app: the document itself never scrolls (panel and list scroll internally)
  document.documentElement.classList.add('covmap-mode');
}

function onModeExit(args) {
  document.documentElement.classList.remove('covmap-mode');
  args.servicesManager.services.coverageService?.stop();
  basicModeInstance.onModeExit.call(this, args);
}

export const coverageLayout = {
  ...basicLayout,
  props: {
    ...basicLayout.props,
    leftPanels: [ohif.thumbnailList],
    leftPanelClosed: true,
    rightPanels: [coveragePanel],
    rightPanelClosed: false,
    rightPanelResizable: true,
    rightPanelInitialExpandedWidth: 360,
    rightPanelMinimumExpandedWidth: 300,
  },
};

export const coverageRoute = {
  ...basicRoute,
  path: 'coverage',
  layoutInstance: coverageLayout,
};

export const modeInstance = {
  ...basicModeInstance,
  id,
  routeName: 'coverage',
  displayName: 'Coverage Map (research)',
  hide: false,
  modeModalities: ['CT'],
  routes: [coverageRoute],
  extensions: extensionDependencies,
  onModeEnter,
  onModeExit,
};

const mode = {
  ...basicMode,
  id,
  modeInstance,
  extensionDependencies,
};

export default mode;
