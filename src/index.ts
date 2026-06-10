/**
 * JupyterLab Share Files Extension - main plugin entry point.
 */

import {
  ILabShell,
  ILayoutRestorer,
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { PathExt } from '@jupyterlab/coreutils';
import { IDocumentManager } from '@jupyterlab/docmanager';
import { IFileBrowserFactory } from '@jupyterlab/filebrowser';
import { Contents } from '@jupyterlab/services';
import { ISettingRegistry } from '@jupyterlab/settingregistry';

import { clearClip, getClip, setClip } from './clipboard';
import { ShareFilesPanel } from './widget';

const PLUGIN_ID = 'jupyterlab_share_files_extension:plugin';
const COMMAND_CREATE_SHARE = 'share-files:create-share';
const FILEBROWSER_SELECTOR = '.jp-DirListing-item';
const CONTENTS_MIME = 'application/x-jupyter-icontents';
const REMOTE_MIME = 'application/x-share-files-remote';
const DROP_TARGET_CLASS = 'jp-mod-dropTarget';
const DIR_ROW_SELECTOR = '.jp-DirListing-item[data-isdir="true"]';

interface IPluginSettings {
  enableShares: boolean;
  enableRequests: boolean;
  showHiddenFiles: boolean;
  tunnelAutostart: boolean;
  pollIntervalSeconds: number;
}

const DEFAULT_SETTINGS: IPluginSettings = {
  enableShares: true,
  enableRequests: true,
  showHiddenFiles: true,
  tunnelAutostart: true,
  pollIntervalSeconds: 15
};

/**
 * Initialization data for the jupyterlab_share_files_extension.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: PLUGIN_ID,
  description:
    'Peer-to-peer file sharing for JupyterLab via named shares and requests.',
  autoStart: true,
  requires: [IFileBrowserFactory, ILabShell, IDocumentManager],
  optional: [ILayoutRestorer, ISettingRegistry],
  activate: (
    app: JupyterFrontEnd,
    factory: IFileBrowserFactory,
    labShell: ILabShell,
    docManager: IDocumentManager,
    restorer: ILayoutRestorer | null,
    settingRegistry: ISettingRegistry | null
  ) => {
    console.log(
      'JupyterLab extension jupyterlab_share_files_extension is activated!'
    );
    const { commands, serviceManager } = app;

    // Helper - get currently selected paths in the file browser (relative paths).
    const getSelectedPaths = (): string[] => {
      const browser = factory.tracker.currentWidget;
      if (!browser) {
        return [];
      }
      const paths: string[] = [];
      for (const item of browser.selectedItems()) {
        if (item && item.path) {
          paths.push(item.path);
        }
      }
      return paths;
    };

    // Helper - current file browser directory (workspace-relative).
    const getCurrentDir = (): string => {
      const browser = factory.tracker.currentWidget;
      return browser?.model.path || '';
    };

    // Helper - navigate the file browser to a directory and select a file.
    const revealInFileBrowser = async (
      dirPath: string,
      name?: string
    ): Promise<void> => {
      const browser = factory.tracker.currentWidget;
      if (!browser) {
        return;
      }
      // Bring the file browser tab to focus first
      labShell.activateById(browser.id);
      await browser.model.cd('/' + dirPath);
      if (name) {
        // Reveal and select the entry once the directory has loaded
        await browser.model.refresh();
        try {
          await (browser as any).selectItemByName?.(name);
        } catch {
          // selectItemByName not available on older JupyterLab - just leave
          // the directory open
        }
      }
    };

    // Create the side panel widget.
    const panel = new ShareFilesPanel({
      serverSettings: serviceManager.serverSettings,
      commands,
      settings: { ...DEFAULT_SETTINGS },
      contents: serviceManager.contents,
      docManager,
      getCurrentDir,
      revealInFileBrowser
    });

    // Patch the file browser so dropping a share entry anywhere in its view
    // (a folder row OR the current directory's empty area) performs a copy.
    // JupyterLab's DirListing only accepts drops on directory rows and treats
    // them as a MOVE - both wrong for us: we must never move files out of a
    // share, and the user expects the current view to be a valid drop target.
    // We intercept in capture phase (before DirListing's bubble-phase handler)
    // and only for drags that originate from our panel (`event.source`), so
    // the file browser's own internal move-drags are untouched.
    const installCopyDrop = (browserNode: HTMLElement): void => {
      // Local own-share entries carry a workspace path (copied locally).
      const isOwnDrag = (event: any): boolean =>
        event.source === panel && !!event.mimeData?.hasData(CONTENTS_MIME);
      // Connected (remote) entries carry REMOTE_MIME; saved server-side.
      const isRemoteDrag = (event: any): boolean =>
        event.source === panel && !!event.mimeData?.hasData(REMOTE_MIME);
      const isPanelDrag = (event: any): boolean =>
        isOwnDrag(event) || isRemoteDrag(event);
      const clearHighlight = (): void => {
        browserNode
          .querySelectorAll('.' + DROP_TARGET_CLASS)
          .forEach(el => el.classList.remove(DROP_TARGET_CLASS));
      };
      const folderRowUnder = (target: EventTarget | null): HTMLElement | null =>
        (target as HTMLElement | null)?.closest(DIR_ROW_SELECTOR) ?? null;

      browserNode.addEventListener(
        'lm-dragenter',
        (event: any) => {
          if (!isPanelDrag(event)) {
            return;
          }
          event.preventDefault();
          event.stopPropagation();
        },
        true
      );
      browserNode.addEventListener(
        'lm-dragover',
        (event: any) => {
          if (!isPanelDrag(event)) {
            return;
          }
          event.preventDefault();
          event.stopPropagation();
          // Local own-share drags use 'move' so the cursor matches the file
          // browser's own glyph (the drop still copies). Remote drags are a
          // genuine download, so they read as 'copy'.
          event.dropAction = isRemoteDrag(event) ? 'copy' : 'move';
          clearHighlight();
          const folder = folderRowUnder(event.target);
          if (folder) {
            folder.classList.add(DROP_TARGET_CLASS);
          }
        },
        true
      );
      browserNode.addEventListener(
        'lm-dragleave',
        (event: any) => {
          if (!isPanelDrag(event)) {
            return;
          }
          clearHighlight();
        },
        true
      );
      browserNode.addEventListener(
        'lm-drop',
        (event: any) => {
          if (!isPanelDrag(event)) {
            return;
          }
          event.preventDefault();
          event.stopPropagation();
          clearHighlight();
          // Destination: a folder row under the cursor, else the current dir.
          let destDir = getCurrentDir();
          const folder = folderRowUnder(event.target);
          if (folder) {
            const name = folder
              .querySelector('.jp-DirListing-itemText')
              ?.textContent?.trim();
            if (name) {
              destDir = PathExt.join(destDir, name);
            }
          }
          if (isRemoteDrag(event)) {
            // Remote (connected) entries: save server-side via the panel.
            const items: Array<{ key: string; name: string; type: string }> =
              event.mimeData.getData(REMOTE_MIME) || [];
            items.forEach(
              it => void panel.saveRemoteEntryTo(it.key, it.name, destDir || '')
            );
            return;
          }
          // Local own-share entries: copy the workspace path directly.
          const paths: string[] = event.mimeData.getData(CONTENTS_MIME) || [];
          const contents: Contents.IManager = serviceManager.contents;
          Promise.all(paths.map(p => contents.copy(p, destDir || ''))).catch(
            err => {
              console.error('share-files: copy to file browser failed', err);
            }
          );
        },
        true
      );
    };

    // Install on every file browser (existing and future).
    factory.tracker.forEach(b => installCopyDrop(b.node));
    factory.tracker.widgetAdded.connect((_, b) => installCopyDrop(b.node));

    // Bridge the native file-browser clipboard, which is private and cannot be
    // read or written from another extension, via the public `commandExecuted`
    // signal. Native copy/cut are mirrored into our own clipboard so the panel
    // can paste them into a share / request; a native paste of a panel-copied
    // entry is performed here (the native paste itself is a no-op because our
    // entries are never in the file browser's own clipboard).
    commands.commandExecuted.connect((_, { id }) => {
      if (id === 'filebrowser:copy' || id === 'filebrowser:cut') {
        const paths = getSelectedPaths();
        if (paths.length) {
          setClip({
            kind: 'local',
            origin: 'fb',
            mode: id === 'filebrowser:cut' ? 'cut' : 'copy',
            paths
          });
        }
        return;
      }
      if (id !== 'filebrowser:paste') {
        return;
      }
      const clip = getClip();
      if (clip?.origin === 'panel') {
        const destDir = getCurrentDir();
        if (clip.kind === 'remote') {
          clip.items.forEach(
            it =>
              void panel.saveRemoteEntryTo(it.connKey, it.name, destDir || '')
          );
        } else {
          void Promise.all(
            clip.paths.map(p => serviceManager.contents.copy(p, destDir || ''))
          ).catch(err => {
            console.error('share-files: paste into file browser failed', err);
          });
        }
        // A panel copy persists for repeat pastes (copy semantics).
      } else if (
        clip?.kind === 'local' &&
        clip.origin === 'fb' &&
        clip.mode === 'cut'
      ) {
        // Native paste just moved the originals - drop the stale mirror so a
        // later panel paste does not try to re-delete already-moved files.
        clearClip();
      }
    });

    labShell.add(panel, 'right', { rank: 600 });
    if (restorer) {
      restorer.add(panel, PLUGIN_ID);
    }

    // Register the file browser context menu command. Visibility honours
    // the user's "enable shares" setting via panel.settings.enableShares.
    commands.addCommand(COMMAND_CREATE_SHARE, {
      label: 'Share Files...',
      caption: 'Create a shareable link for the selected files or folders',
      execute: () => {
        const paths = getSelectedPaths();
        if (paths.length === 0) {
          return;
        }
        labShell.activateById(panel.id);
        void panel.createShareFlow(paths);
      },
      isEnabled: () =>
        panel.settings.enableShares && getSelectedPaths().length > 0,
      isVisible: () =>
        panel.settings.enableShares && getSelectedPaths().length > 0
    });

    app.contextMenu.addItem({
      command: COMMAND_CREATE_SHARE,
      selector: FILEBROWSER_SELECTOR,
      rank: 12
    });

    // Wire the settings registry so toggles take effect live.
    if (settingRegistry) {
      const applySettings = (settings: ISettingRegistry.ISettings): void => {
        const next: IPluginSettings = {
          enableShares: settings.get('enableShares').composite as boolean,
          enableRequests: settings.get('enableRequests').composite as boolean,
          showHiddenFiles: settings.get('showHiddenFiles').composite as boolean,
          tunnelAutostart: settings.get('tunnelAutostart').composite as boolean,
          pollIntervalSeconds:
            (settings.get('pollIntervalSeconds').composite as number) || 15
        };
        panel.updateSettings(next);
      };
      settingRegistry
        .load(PLUGIN_ID)
        .then(settings => {
          applySettings(settings);
          settings.changed.connect(applySettings);
        })
        .catch(reason => {
          console.warn(
            `Failed to load settings for ${PLUGIN_ID}: ${reason}. ` +
              'Using defaults (both features enabled).'
          );
        });
    }
  }
};

export default plugin;
