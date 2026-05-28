import { LabIcon } from '@jupyterlab/ui-components';

const shareSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M5 4v2h14V4H5zm0 10h4v6h6v-6h4l-7-7-7 7z"/>
</svg>`;

const inboxSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M19 3H4.99c-1.11 0-1.98.9-1.98 2L3 19c0 1.1.88 2 1.99 2H19c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 12h-4c0 1.66-1.35 3-3 3s-3-1.34-3-3H4.99V5H19v10z"/>
</svg>`;

const folderSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/>
</svg>`;

const fileSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm-1 7V3.5L18.5 9H13z"/>
</svg>`;

const copySvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/>
</svg>`;

const closeSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
</svg>`;

const refreshSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/>
</svg>`;

const addSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/>
</svg>`;

const trashSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>
</svg>`;

const downloadSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
</svg>`;

const linkSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="jp-icon3">
<path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/>
</svg>`;

export const shareIcon = new LabIcon({
  name: 'share-files:share',
  svgstr: shareSvg
});
export const inboxIcon = new LabIcon({
  name: 'share-files:inbox',
  svgstr: inboxSvg
});
export const folderIcon = new LabIcon({
  name: 'share-files:folder',
  svgstr: folderSvg
});
export const fileIcon = new LabIcon({
  name: 'share-files:file',
  svgstr: fileSvg
});
export const copyIcon = new LabIcon({
  name: 'share-files:copy',
  svgstr: copySvg
});
export const closeIcon = new LabIcon({
  name: 'share-files:close',
  svgstr: closeSvg
});
export const refreshIcon = new LabIcon({
  name: 'share-files:refresh',
  svgstr: refreshSvg
});
export const addIcon = new LabIcon({ name: 'share-files:add', svgstr: addSvg });
export const trashIcon = new LabIcon({
  name: 'share-files:trash',
  svgstr: trashSvg
});
export const downloadIcon = new LabIcon({
  name: 'share-files:download',
  svgstr: downloadSvg
});
export const linkIcon = new LabIcon({
  name: 'share-files:link',
  svgstr: linkSvg
});
