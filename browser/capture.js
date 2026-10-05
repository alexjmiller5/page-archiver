import { getPageData, helper } from 'single-file-core/single-file.js';

// Only URL input crosses this binding. The host never forwards page headers,
// cookies, hub credentials or caller-supplied fetch options.
async function fetchResource(url) {
  if (/^(data|blob):/i.test(url)) return fetch(url);
  const response = await globalThis.__archiveResource(url);
  const bytes = Uint8Array.from(atob(response.body), c => c.charCodeAt(0));
  return {
    status: response.status,
    url: response.url,
    headers: new Headers({ 'content-type': response.mime }),
    arrayBuffer: async () => bytes.buffer,
  };
}

globalThis.__archivePage = async () => {
  const data = await getPageData({
    compressHTML: false,
    compressContent: false,
    removeFrames: true,
    blockScripts: true,
    blockVideos: true,
    blockAudios: true,
    removeHiddenElements: true,
    removeUnusedStyles: true,
    removeUnusedFonts: true,
    removeAlternativeImages: true,
    removeAlternativeFonts: true,
    removeAlternativeMedias: false,
    groupDuplicateImages: true,
    loadDeferredContent: true,
    loadDeferredContentMaxIdleTime: 1500,
    loadDeferredContentMaxIdleTimeAfterLoad: 1500,
    loadDeferredContentBeforeFrames: true,
    insertMetaCSP: true,
    insertCanonicalLink: true,
    saveOriginalURLs: false,
    networkTimeout: 10000,
    maxResourceSizeEnabled: true,
    maxResourceSize: 20,
  }, { fetch: fetchResource });
  return data.content;
};

globalThis.__archiveFinalize = content => {
  // SingleFile can add its own layout-repair script even with blockScripts.
  // Apply that repair now to an inert document, then retain no runnable script.
  // This does not make arbitrary archived HTML trusted viewer content.
  const saved = new DOMParser().parseFromString(content, 'text/html');
  helper.fixInvalidNesting(saved, helper.NESTING_TRACK_ID_ATTRIBUTE_NAME);
  saved.querySelectorAll('script,meta[http-equiv="refresh"],meta[http-equiv="content-security-policy"]').forEach(element => element.remove());
  const policy = saved.createElement('meta');
  policy.httpEquiv = 'Content-Security-Policy';
  policy.content = "default-src 'none'; img-src data:; style-src 'unsafe-inline' data:; font-src data:; media-src data:; form-action 'none'; base-uri 'none'";
  saved.head.prepend(policy);
  return '<!DOCTYPE html>\n' + saved.documentElement.outerHTML;
};
