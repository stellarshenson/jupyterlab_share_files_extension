/**
 * JupyterLab Share Files Extension - main plugin entry point.
 */

import {
  ILabShell,
  ILayoutRestorer,
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { IFileBrowserFactory } from '@jupyterlab/filebrowser';
import { ISettingRegistry } from '@jupyterlab/settingregistry';

import { ShareFilesPanel } from './widget';

const PLUGIN_ID = 'jupyterlab_share_files_extension:plugin';
const COMMAND_CREATE_SHARE = 'share-files:create-share';
const FILEBROWSER_SELECTOR = '.jp-DirListing-item';

interface IPluginSettings {
  enableShares: boolean;
  enableRequests: boolean;
}

const DEFAULT_SETTINGS: IPluginSettings = {
  enableShares: true,
  enableRequests: true
};

/**
 * Initialization data for the jupyterlab_share_files_extension.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: PLUGIN_ID,
  description:
    'Peer-to-peer file sharing for JupyterLab via named shares and requests.',
  autoStart: true,
  requires: [IFileBrowserFactory, ILabShell],
  optional: [ILayoutRestorer, ISettingRegistry],
  activate: (
    app: JupyterFrontEnd,
    factory: IFileBrowserFactory,
    labShell: ILabShell,
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

    // Create the side panel widget.
    const panel = new ShareFilesPanel({
      serverSettings: serviceManager.serverSettings,
      commands,
      settings: { ...DEFAULT_SETTINGS }
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
          enableRequests: settings.get('enableRequests').composite as boolean
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
