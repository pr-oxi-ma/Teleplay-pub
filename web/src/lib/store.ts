/**
 * AppState management using Zustand
 */
import { create } from "zustand";
import { TelegramFile, Folder, isImageFile, isTimedMediaFile } from "./api";

export type ActiveSection = "files" | "recent" | "continue_watching";
export type NavigationSection = ActiveSection | "recycle_bin" | "analytics";
export type ViewMode = "grid" | "list";
export type FileTypeFilter = "video" | "audio" | "image" | "document" | null;
export type PlaybackMode =
  | "normal"
  | "repeat_one"
  | "shuffle"
  | "autoplay_off"
  | "stop_after_this";

export interface AppSettings {
  defaultStartSection: ActiveSection;
  defaultViewMode: ViewMode;
  defaultFileTypeFilter: FileTypeFilter;
  showVideoResolution: boolean;
  resumePlayback: boolean;
  defaultPlaybackSpeed: number;
  playbackMode: PlaybackMode;
}

export interface ToastNotification {
  id: string;
  message: string;
  type: "success" | "error" | "info";
  action?: {
    label: string;
    onClick: () => void | Promise<void>;
  };
  duration?: number;
}

const APP_SETTINGS_STORAGE_KEY = "teleplay_app_settings";
const LEGACY_PLAYBACK_MODE_STORAGE_KEY = "teleplay_playback_mode";

export const PLAYBACK_SPEED_OPTIONS = [0.5, 1, 1.25, 1.5, 2] as const;
export const DEFAULT_APP_SETTINGS: AppSettings = {
  defaultStartSection: "files",
  defaultViewMode: "grid",
  defaultFileTypeFilter: null,
  showVideoResolution: true,
  resumePlayback: true,
  defaultPlaybackSpeed: 1,
  playbackMode: "normal",
};

const sectionTitle = (section: NavigationSection): string =>
  section === "files"
    ? "My Files"
    : section === "recent"
      ? "Recently Added"
      : section === "continue_watching"
        ? "Continue Watching"
        : section === "recycle_bin"
          ? "Recycle Bin"
          : "Storage Analytics";

const isActiveSection = (value: unknown): value is ActiveSection =>
  value === "files" || value === "recent" || value === "continue_watching";

const isViewMode = (value: unknown): value is ViewMode =>
  value === "grid" || value === "list";

const isFileTypeFilter = (value: unknown): value is FileTypeFilter =>
  value === null ||
  value === "video" ||
  value === "audio" ||
  value === "image" ||
  value === "document";

const isPlaybackMode = (value: unknown): value is PlaybackMode =>
  value === "normal" ||
  value === "repeat_one" ||
  value === "shuffle" ||
  value === "autoplay_off" ||
  value === "stop_after_this";

const isPlaybackSpeed = (value: unknown): value is number =>
  typeof value === "number" &&
  PLAYBACK_SPEED_OPTIONS.some((speed) => speed === value);

const normalizeSettings = (value: Partial<AppSettings>): AppSettings => ({
  defaultStartSection: isActiveSection(value.defaultStartSection)
    ? value.defaultStartSection
    : DEFAULT_APP_SETTINGS.defaultStartSection,
  defaultViewMode: isViewMode(value.defaultViewMode)
    ? value.defaultViewMode
    : DEFAULT_APP_SETTINGS.defaultViewMode,
  defaultFileTypeFilter: isFileTypeFilter(value.defaultFileTypeFilter)
    ? value.defaultFileTypeFilter
    : DEFAULT_APP_SETTINGS.defaultFileTypeFilter,
  showVideoResolution:
    typeof value.showVideoResolution === "boolean"
      ? value.showVideoResolution
      : DEFAULT_APP_SETTINGS.showVideoResolution,
  resumePlayback:
    typeof value.resumePlayback === "boolean"
      ? value.resumePlayback
      : DEFAULT_APP_SETTINGS.resumePlayback,
  defaultPlaybackSpeed: isPlaybackSpeed(value.defaultPlaybackSpeed)
    ? value.defaultPlaybackSpeed
    : DEFAULT_APP_SETTINGS.defaultPlaybackSpeed,
  playbackMode: isPlaybackMode(value.playbackMode)
    ? value.playbackMode
    : DEFAULT_APP_SETTINGS.playbackMode,
});

const loadAppSettings = (): AppSettings => {
  try {
    const stored = localStorage.getItem(APP_SETTINGS_STORAGE_KEY);
    const parsed = stored ? JSON.parse(stored) : {};
    const legacyPlaybackMode = localStorage.getItem(
      LEGACY_PLAYBACK_MODE_STORAGE_KEY,
    );

    return normalizeSettings({
      ...parsed,
      playbackMode: isPlaybackMode(parsed.playbackMode)
        ? parsed.playbackMode
        : legacyPlaybackMode,
    });
  } catch {
    return DEFAULT_APP_SETTINGS;
  }
};

const saveAppSettings = (settings: AppSettings) => {
  try {
    localStorage.setItem(APP_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
    localStorage.setItem(
      LEGACY_PLAYBACK_MODE_STORAGE_KEY,
      settings.playbackMode,
    );
  } catch {
    // Ignore storage errors and keep the in-memory settings.
  }
};

const initialSettings = loadAppSettings();

interface AppState {
  // Current navigation
  currentFolderId: number | null;
  setCurrentFolderId: (id: number | null) => void;

  // Breadcrumb path
  breadcrumbs: Array<{ id: number | null; name: string }>;
  setBreadcrumbs: (
    breadcrumbs: Array<{ id: number | null; name: string }>,
  ) => void;

  // Selection
  selectedFileIds: Set<number>;
  selectedFolderIds: Set<number>;
  selectFile: (id: number, multi?: boolean) => void;
  deselectFile: (id: number) => void;
  selectFolder: (id: number, multi?: boolean) => void;
  deselectFolder: (id: number) => void;
  clearSelection: () => void;
  selectAll: (fileIds: number[], folderIds?: number[]) => void;

  // View mode
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;

  // App settings
  appSettings: AppSettings;
  updateAppSettings: (settings: Partial<AppSettings>) => void;
  resetAppSettings: () => void;

  // Modals
  previewFile: TelegramFile | null;
  setPreviewFile: (file: TelegramFile | null) => void;

  renameFile: TelegramFile | null;
  setRenameFile: (file: TelegramFile | null) => void;

  renameFolder: Folder | null;
  setRenameFolder: (folder: Folder | null) => void;

  moveItems: { files: TelegramFile[]; folders: Folder[] } | null;
  setMoveItems: (
    items: { files: TelegramFile[]; folders: Folder[] } | null,
  ) => void;
  setMoveFiles: (files: TelegramFile[]) => void;

  showNewFolder: boolean;
  setShowNewFolder: (show: boolean) => void;

  deleteConfirm: {
    type: "file" | "folder" | "multiple";
    items: (TelegramFile | Folder)[];
  } | null;
  setDeleteConfirm: (
    item: {
      type: "file" | "folder" | "multiple";
      items: (TelegramFile | Folder)[];
    } | null,
  ) => void;

  // Clipboard
  clipboard: {
    mode: "copy" | "cut";
    files: TelegramFile[];
    folders: Folder[];
  } | null;
  setClipboard: (
    clipboard: {
      mode: "copy" | "cut";
      files: TelegramFile[];
      folders: Folder[];
    } | null,
  ) => void;

  // Player state
  isPlayerMinimized: boolean;
  setPlayerMinimized: (minimized: boolean) => void;
  mediaQueue: TelegramFile[];
  setMediaQueue: (files: TelegramFile[]) => void;
  activeAudioFileId: number | null;
  activeAudioIsPlaying: boolean;
  setActiveAudioPlayback: (fileId: number | null, isPlaying?: boolean) => void;
  activeVideoFileId: number | null;
  activeVideoIsPlaying: boolean;
  setActiveVideoPlayback: (fileId: number | null, isPlaying?: boolean) => void;

  // Image viewer state - separate from audio/video so music can continue while images are open
  imageViewerFile: TelegramFile | null;
  setImageViewerFile: (file: TelegramFile | null) => void;
  imageQueue: TelegramFile[];
  setImageQueue: (files: TelegramFile[]) => void;

  // Search
  searchQuery: string;
  setSearchQuery: (query: string) => void;

  // Filter
  fileTypeFilter: FileTypeFilter;
  setFileTypeFilter: (type: FileTypeFilter) => void;

  // Context menu - only one can be open at a time, with position for fixed positioning
  activeContextMenu:
    | { type: "file"; item: TelegramFile; x: number; y: number }
    | { type: "folder"; item: Folder; x: number; y: number }
    | null;
  setActiveContextMenu: (
    menu:
      | { type: "file"; item: TelegramFile; x: number; y: number }
      | { type: "folder"; item: Folder; x: number; y: number }
      | null,
  ) => void;
  selectedFiles: TelegramFile[];
  setSelectedFiles: (files: TelegramFile[]) => void;

  // Navigation Section
  activeSection: NavigationSection;
  setActiveSection: (section: NavigationSection) => void;

  // Toast Notifications
  toasts: ToastNotification[];
  addToast: (
    message: string,
    type?: "success" | "error" | "info",
    action?: ToastNotification["action"],
    duration?: number,
  ) => void;
  removeToast: (id: string) => void;

  // Drag Selection Box
  selectionBox: {
    x1: number;
    y1: number;
    x2: number;
    y2: number;
    active: boolean;
  } | null;
  setSelectionBox: (
    box: {
      x1: number;
      y1: number;
      x2: number;
      y2: number;
      active: boolean;
    } | null,
  ) => void;
}

export const useAppStore = create<AppState>((set) => ({
  // Navigation
  currentFolderId: null,
  setCurrentFolderId: (id) => set({ currentFolderId: id }),

  // Breadcrumbs
  breadcrumbs: [
    { id: null, name: sectionTitle(initialSettings.defaultStartSection) },
  ],
  setBreadcrumbs: (breadcrumbs) => set({ breadcrumbs }),

  // Navigation Section
  activeSection: initialSettings.defaultStartSection,
  setActiveSection: (section) =>
    set({
      activeSection: section,
      currentFolderId: null,
      breadcrumbs: [{ id: null, name: sectionTitle(section) }],
      selectedFileIds: new Set(),
      selectedFolderIds: new Set(),
    }),

  // Selection
  selectedFileIds: new Set(),
  selectedFolderIds: new Set(),
  selectFile: (id, multi = false) =>
    set((state) => {
      if (multi) {
        const newSet = new Set(state.selectedFileIds);
        if (newSet.has(id)) newSet.delete(id);
        else newSet.add(id);
        return { selectedFileIds: newSet };
      }
      return { selectedFileIds: new Set([id]), selectedFolderIds: new Set() };
    }),
  deselectFile: (id) =>
    set((state) => {
      const newSet = new Set(state.selectedFileIds);
      newSet.delete(id);
      return { selectedFileIds: newSet };
    }),
  selectFolder: (id, multi = false) =>
    set((state) => {
      if (multi) {
        const newSet = new Set(state.selectedFolderIds);
        if (newSet.has(id)) newSet.delete(id);
        else newSet.add(id);
        return { selectedFolderIds: newSet };
      }
      return { selectedFolderIds: new Set([id]), selectedFileIds: new Set() };
    }),
  deselectFolder: (id) =>
    set((state) => {
      const newSet = new Set(state.selectedFolderIds);
      newSet.delete(id);
      return { selectedFolderIds: newSet };
    }),
  clearSelection: () =>
    set({ selectedFileIds: new Set(), selectedFolderIds: new Set() }),
  selectAll: (fileIds, folderIds = []) =>
    set({
      selectedFileIds: new Set(fileIds),
      selectedFolderIds: new Set(folderIds),
    }),

  // View mode
  viewMode: initialSettings.defaultViewMode,
  setViewMode: (mode) => set({ viewMode: mode }),

  // App settings
  appSettings: initialSettings,
  updateAppSettings: (settings) =>
    set((state) => {
      const nextSettings = normalizeSettings({
        ...state.appSettings,
        ...settings,
      });
      saveAppSettings(nextSettings);

      const nextState: Partial<AppState> = { appSettings: nextSettings };

      if (settings.defaultViewMode) {
        nextState.viewMode = nextSettings.defaultViewMode;
      }

      if (settings.defaultStartSection) {
        nextState.activeSection = nextSettings.defaultStartSection;
        nextState.currentFolderId = null;
        nextState.breadcrumbs = [
          { id: null, name: sectionTitle(nextSettings.defaultStartSection) },
        ];
      }

      if (
        Object.prototype.hasOwnProperty.call(settings, "defaultFileTypeFilter")
      ) {
        nextState.fileTypeFilter = nextSettings.defaultFileTypeFilter;
      }

      return nextState;
    }),
  resetAppSettings: () =>
    set(() => {
      saveAppSettings(DEFAULT_APP_SETTINGS);
      return {
        appSettings: DEFAULT_APP_SETTINGS,
        viewMode: DEFAULT_APP_SETTINGS.defaultViewMode,
        activeSection: DEFAULT_APP_SETTINGS.defaultStartSection,
        fileTypeFilter: DEFAULT_APP_SETTINGS.defaultFileTypeFilter,
        currentFolderId: null,
        breadcrumbs: [
          {
            id: null,
            name: sectionTitle(DEFAULT_APP_SETTINGS.defaultStartSection),
          },
        ],
      };
    }),

  // Modals
  previewFile: null,
  setPreviewFile: (file) => set({ previewFile: file }),

  renameFile: null,
  setRenameFile: (file) => set({ renameFile: file }),

  renameFolder: null,
  setRenameFolder: (folder) => set({ renameFolder: folder }),

  moveItems: null,
  setMoveItems: (items) => set({ moveItems: items }),
  setMoveFiles: (files) => set({ moveItems: { files, folders: [] } }),
  selectedFiles: [],
  setSelectedFiles: (files) => set({ selectedFiles: files }),

  showNewFolder: false,
  setShowNewFolder: (show) => set({ showNewFolder: show }),

  deleteConfirm: null,
  setDeleteConfirm: (item) => set({ deleteConfirm: item }),

  // Clipboard
  clipboard: null,
  setClipboard: (clipboard) => set({ clipboard }),

  // Player state
  isPlayerMinimized: false,
  setPlayerMinimized: (minimized) => set({ isPlayerMinimized: minimized }),
  mediaQueue: [],
  setMediaQueue: (files) => set({ mediaQueue: files.filter(isTimedMediaFile) }),
  activeAudioFileId: null,
  activeAudioIsPlaying: false,
  setActiveAudioPlayback: (fileId, isPlaying = false) =>
    set({
      activeAudioFileId: fileId,
      activeAudioIsPlaying: !!fileId && isPlaying,
    }),
  activeVideoFileId: null,
  activeVideoIsPlaying: false,
  setActiveVideoPlayback: (fileId, isPlaying = false) =>
    set({
      activeVideoFileId: fileId,
      activeVideoIsPlaying: !!fileId && isPlaying,
    }),

  // Image viewer state - separate from audio/video so music can continue while images are open
  imageViewerFile: null,
  setImageViewerFile: (file) => set({ imageViewerFile: file }),
  imageQueue: [],
  setImageQueue: (files) => set({ imageQueue: files.filter(isImageFile) }),

  // Search
  searchQuery: "",
  setSearchQuery: (query) => set({ searchQuery: query }),

  // Filter
  fileTypeFilter: initialSettings.defaultFileTypeFilter,
  setFileTypeFilter: (type) => set({ fileTypeFilter: type }),

  // Context menu - only one can be open at a time
  activeContextMenu: null,
  setActiveContextMenu: (menu) => set({ activeContextMenu: menu }),

  // Toast Notifications
  toasts: [],
  addToast: (message, type = "success", action, duration) =>
    set((state) => {
      const id = Math.random().toString(36).substring(2, 9);
      return { toasts: [...state.toasts, { id, message, type, action, duration }] };
    }),
  removeToast: (id) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),

  // Drag Selection Box
  selectionBox: null,
  setSelectionBox: (box) => set({ selectionBox: box }),
}));
