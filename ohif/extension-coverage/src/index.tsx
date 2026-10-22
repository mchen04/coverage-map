import React from 'react';
import { id } from './id';
import CoverageService from './CoverageService';
import getCommandsModule from './commandsModule';
import CoveragePanel from './panel/CoveragePanel';

export { CoverageService };
export { PRESETS } from './presets';

/**
 * Coverage Map extension (research use only): Cornerstone3D reading instrumentation,
 * the coverage panel with the vtk.js organ map, and coverage-mode commands. The
 * instrumentation only runs while the coverage mode has started it.
 */
const extension = {
  id,

  preRegistration({ servicesManager }) {
    servicesManager.registerService(CoverageService.REGISTRATION);
  },

  getPanelModule({ servicesManager, commandsManager, extensionManager }) {
    const WrappedCoveragePanel = () => (
      <CoveragePanel
        servicesManager={servicesManager}
        commandsManager={commandsManager}
        extensionManager={extensionManager}
      />
    );
    return [
      {
        name: 'coverage',
        iconName: 'tab-linear',
        iconLabel: 'Coverage Map',
        label: 'Coverage Map',
        component: WrappedCoveragePanel,
      },
    ];
  },

  getCommandsModule,
};

export default extension;
