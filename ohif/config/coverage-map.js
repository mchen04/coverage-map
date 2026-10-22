/** Coverage Map app config (research use only, not a medical device).
 *  One same-origin DICOMweb source: the covmap server proxies /dicom-web and /wado to
 *  Orthanc, and serves the coverage API under /api. Built in by scripts/build_ohif.sh. */
window.config = {
  routerBasename: null,
  showStudyList: true,
  extensions: [],
  modes: [],
  showWarningMessageForCrossOrigin: false,
  showCPUFallbackMessage: true,
  showLoadingIndicator: true,
  strictZSpacingForVolumeViewport: true,
  maxNumberOfWebWorkers: 3,
  investigationalUseDialog: { option: 'never' },
  defaultDataSourceName: 'orthanc',
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'orthanc',
      configuration: {
        friendlyName: 'Coverage Map Orthanc',
        name: 'orthanc',
        wadoUriRoot: '/wado',
        qidoRoot: '/dicom-web',
        wadoRoot: '/dicom-web',
        qidoSupportsIncludeField: false,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: true,
        staticWado: false,
        dicomUploadEnabled: false,
        omitQuotationForMultipartRequest: true,
        bulkDataURI: {
          enabled: true,
          // Orthanc writes absolute BulkDataURIs with its own host; keep them same-origin.
          transform: url => url.replace(/^https?:\/\/[^/]+/, ''),
        },
      },
    },
  ],
  covmap: {
    apiRoot: '/api',
  },
  httpErrorHandler: error => {
    console.warn(`HTTP Error Handler (status: ${error.status})`, error);
  },
};
