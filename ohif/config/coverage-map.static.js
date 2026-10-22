window.config = {
  routerBasename: null,
  showStudyList: false,
  extensions: [],
  modes: [],
  showWarningMessageForCrossOrigin: false,
  showCPUFallbackMessage: true,
  showLoadingIndicator: true,
  strictZSpacingForVolumeViewport: true,
  maxNumberOfWebWorkers: 3,
  investigationalUseDialog: { option: 'never' },
  defaultDataSourceName: 'dicomjson',
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomjson',
      sourceName: 'dicomjson',
      configuration: {
        friendlyName: 'Coverage Map scans',
        name: 'json',
      },
    },
  ],
  covmap: {
    apiRoot: '/api',
    inBrowser: {
      worker: '/covmap-server-worker.js',
      dataRoot: '/data',
      pythonZip: '/py/covmap.zip',
      pyodideUrl: 'https://cdn.jsdelivr.net/pyodide/v0.28.3/full/',
      demoCase: 'pancreas-0051',
    },
  },
  httpErrorHandler: error => {
    console.warn(`HTTP Error Handler (status: ${error.status})`, error);
  },
};
