import { useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CheckSquare,
  ChevronRight,
  Eye,
  FileText,
  Film,
  Folder,
  Image as ImageIcon,
  LockKeyhole,
  Music,
  Play,
  RefreshCw,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import {
  formatDuration,
  formatFileSize,
  getVideoResolutionLabel,
  isImageFile,
  isTimedMediaFile,
  mediaCrossOrigin,
  TelegramFile,
  toApiUrl,
  TrashSelection,
  useBulkDeleteTrash,
  useBulkRestoreTrash,
  useEmptyTrash,
  useTrash,
  useTrashFolder,
} from "../lib/api";
import { useAppStore } from "../lib/store";

interface RecycleBinProps {
  searchQuery: string;
}

type SelectedKey = `file:${number}` | `folder:${number}`;

const fileIcon = (file: TelegramFile) => {
  if (file.file_type === "video") return Film;
  if (file.file_type === "audio") return Music;
  if (isImageFile(file)) return ImageIcon;
  return FileText;
};

const expiryLabel = (value?: string | null) => {
  if (!value) return "Expiry unavailable";
  const expiry = new Date(value);
  const days = Math.max(0, Math.ceil((expiry.getTime() - Date.now()) / 86_400_000));
  if (days === 0) return "Deletes today";
  if (days === 1) return "1 day remaining";
  return `${days} days remaining`;
};

const exactExpiry = (value?: string | null) =>
  value
    ? `Expires ${new Intl.DateTimeFormat(undefined, {
        day: "numeric",
        month: "short",
        year: "numeric",
      }).format(new Date(value))}`
    : "Expiry unknown";

function TrashFileThumbnail({
  file,
  onOpen,
  isActiveMedia,
  activeMediaIsPlaying,
}: {
  file: TelegramFile;
  onOpen: () => void;
  isActiveMedia: boolean;
  activeMediaIsPlaying: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const Icon = fileIcon(file);
  const thumbnailUrl = !failed && file.thumbnail_url ? toApiUrl(file.thumbnail_url) : null;

  return (
    <button
      type="button"
      onClick={onOpen}
      className={`relative flex h-14 w-20 shrink-0 items-center justify-center overflow-hidden rounded-xl border bg-dark-800 text-dark-300 transition-colors hover:text-primary-200 ${
        isActiveMedia
          ? "border-primary-300/60 shadow-md shadow-primary-500/20"
          : "border-white/[0.06] hover:border-primary-400/30"
      }`}
      title={`Open ${file.file_name}`}
    >
      {thumbnailUrl ? (
        <img
          src={thumbnailUrl}
          alt=""
          crossOrigin={mediaCrossOrigin}
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => setFailed(true)}
          className="h-full w-full object-cover"
        />
      ) : (
        <Icon className="h-6 w-6" />
      )}
      {file.last_pos && file.duration && file.last_pos / file.duration > 0.05 && (
        <span className="absolute inset-x-0 bottom-0 h-1 bg-black/50">
          <span
            className="block h-full bg-primary-500"
            style={{
              width: `${Math.min(100, (file.last_pos / file.duration) * 100)}%`,
            }}
          />
        </span>
      )}
      {isActiveMedia && (
        <span className="absolute left-1 top-1 flex h-5 w-5 items-center justify-center rounded-full border border-primary-300/40 bg-black/60 text-primary-100 backdrop-blur">
          <Play
            className={`h-2.5 w-2.5 ${activeMediaIsPlaying ? "animate-pulse" : ""}`}
            fill="currentColor"
          />
        </span>
      )}
      <span className="absolute inset-0 flex items-center justify-center bg-black/35 opacity-0 transition-opacity hover:opacity-100">
        <Eye className="h-5 w-5 text-white" />
      </span>
    </button>
  );
}

export default function RecycleBin({ searchQuery }: RecycleBinProps) {
  const rootQuery = useTrash();
  const [currentFolderId, setCurrentFolderId] = useState<number | null>(null);
  const folderQuery = useTrashFolder(currentFolderId);
  const restoreMutation = useBulkRestoreTrash();
  const deleteMutation = useBulkDeleteTrash();
  const emptyMutation = useEmptyTrash();
  const {
    addToast,
    setPreviewFile,
    setMediaQueue,
    setImageQueue,
    setImageViewerFile,
    activeAudioFileId,
    activeAudioIsPlaying,
    activeVideoFileId,
    activeVideoIsPlaying,
    appSettings,
  } = useAppStore();
  const [selected, setSelected] = useState<Set<SelectedKey>>(new Set());
  const [confirmAction, setConfirmAction] = useState<"delete" | "empty" | null>(null);

  const activeQuery = currentFolderId === null ? rootQuery : folderQuery;
  const data = currentFolderId === null ? rootQuery.data : folderQuery.data;
  const isLoading = activeQuery.isLoading;
  const isFetching = activeQuery.isFetching;

  const filtered = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return {
      files: (data?.files || []).filter(
        (item) => !query || item.file_name.toLowerCase().includes(query),
      ),
      folders: (data?.folders || []).filter(
        (item) => !query || item.name.toLowerCase().includes(query),
      ),
    };
  }, [data, searchQuery]);

  const visibleKeys: SelectedKey[] = [
    ...filtered.folders.map((item) => `folder:${item.id}` as SelectedKey),
    ...filtered.files.map((item) => `file:${item.id}` as SelectedKey),
  ];

  const selectionPayload = (keys = selected): TrashSelection => {
    const payload: TrashSelection = { file_ids: [], folder_ids: [] };
    keys.forEach((key) => {
      const [type, rawId] = key.split(":");
      const id = Number(rawId);
      if (type === "file") payload.file_ids.push(id);
      else payload.folder_ids.push(id);
    });
    return payload;
  };

  const toggleSelected = (key: SelectedKey) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const navigateToFolder = (folderId: number | null) => {
    setSelected(new Set());
    setCurrentFolderId(folderId);
  };

  const openFile = (file: TelegramFile) => {
    if (isImageFile(file)) {
      // Recycle Bin previews are intentionally single-item. This keeps image
      // navigation/prefetch disabled while preserving open/download/fullscreen.
      setImageQueue([file]);
      setImageViewerFile(file);
      return;
    }
    if (isTimedMediaFile(file)) {
      // Keep the regular player and resume/miniplayer behavior, but do not build
      // a previous/next queue for deleted media.
      setMediaQueue([file]);
      setPreviewFile(file);
      return;
    }

    // Documents open through the authenticated, read-only trash stream route.
    window.open(toApiUrl(file.stream_url), "_blank", "noopener,noreferrer");
  };

  const restore = async (keys = selected) => {
    if (!keys.size) return;
    try {
      await restoreMutation.mutateAsync(selectionPayload(keys));
      setSelected(new Set());
      addToast(`${keys.size} item${keys.size === 1 ? "" : "s"} restored`, "success");
    } catch (error: any) {
      addToast(error?.response?.data?.detail || "Restore failed", "error");
    }
  };

  const permanentlyDelete = async () => {
    try {
      if (confirmAction === "empty") {
        await emptyMutation.mutateAsync();
        navigateToFolder(null);
        addToast("Recycle Bin emptied", "success");
      } else {
        await deleteMutation.mutateAsync(selectionPayload());
        addToast("Selected items permanently deleted", "success");
      }
      setSelected(new Set());
      setConfirmAction(null);
    } catch (error: any) {
      addToast(error?.response?.data?.detail || "Permanent delete failed", "error");
    }
  };

  const selectAllVisible = () => {
    const allSelected =
      visibleKeys.length > 0 && visibleKeys.every((key) => selected.has(key));
    setSelected(allSelected ? new Set() : new Set(visibleKeys));
  };

  const refetchActive = () => {
    if (currentFolderId === null) rootQuery.refetch();
    else folderQuery.refetch();
  };

  const breadcrumbs = currentFolderId === null ? [] : folderQuery.data?.breadcrumbs || [];

  if (isLoading) {
    return (
      <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {[...Array(6)].map((_, index) => (
          <div
            key={index}
            className="h-32 animate-pulse rounded-2xl border border-white/[0.04] bg-dark-800/50"
          />
        ))}
      </div>
    );
  }

  return (
    <div className="mx-auto w-full min-w-0 max-w-6xl animate-fade-in pb-20">
      <div className="mb-4 flex min-w-0 items-start gap-3 rounded-xl border border-blue-500/15 bg-blue-500/[0.06] p-3 text-sm text-blue-100">
        <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-blue-300" />
        <p className="min-w-0 leading-relaxed">
          <strong>Read-only mode.</strong> You can open deleted folders, preview files,
          select individual items, restore them, or permanently delete them.
        </p>
      </div>

      <div className="mb-5 flex min-w-0 flex-col gap-4 overflow-hidden rounded-2xl border border-white/[0.06] bg-dark-900/60 p-4 shadow-xl shadow-black/10 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="rounded-xl border border-red-500/20 bg-red-500/10 p-2.5 text-red-300">
            <Trash2 className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-lg font-bold text-white">
              {folderQuery.data?.current_folder.name || "Recycle Bin"}
            </h1>
            <p className="mt-1 text-sm text-dark-400">
              Restore items before their retention period expires.
            </p>
          </div>
        </div>
        <div className="flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto">
          <button
            onClick={refetchActive}
            className="inline-flex flex-1 items-center justify-center gap-2 rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-dark-200 transition-colors hover:bg-white/[0.06] hover:text-white sm:flex-none"
          >
            <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
            Refresh
          </button>
          {currentFolderId === null && (
            <button
              onClick={() => setConfirmAction("empty")}
              disabled={!rootQuery.data?.total}
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm font-semibold text-red-300 transition-colors hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-40 sm:flex-none"
            >
              <Trash2 className="h-4 w-4" />
              Empty Bin
            </button>
          )}
        </div>
      </div>

      {currentFolderId !== null && (
        <nav className="mb-4 flex min-w-0 items-center gap-1 overflow-x-auto rounded-xl border border-white/[0.06] bg-dark-900/45 p-2 no-scrollbar">
          <button
            onClick={() => navigateToFolder(null)}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-2 text-sm text-dark-300 transition-colors hover:bg-white/[0.06] hover:text-white"
          >
            <Trash2 className="h-4 w-4" />
            Recycle Bin
          </button>
          {breadcrumbs.map((crumb, index) => (
            <div key={crumb.id} className="flex shrink-0 items-center gap-1">
              <ChevronRight className="h-4 w-4 text-dark-600" />
              <button
                onClick={() => navigateToFolder(crumb.id)}
                className={`max-w-[180px] truncate rounded-lg px-2.5 py-2 text-sm transition-colors ${
                  index === breadcrumbs.length - 1
                    ? "bg-white/[0.07] font-semibold text-white"
                    : "text-dark-300 hover:bg-white/[0.06] hover:text-white"
                }`}
                title={crumb.name}
              >
                {crumb.name}
              </button>
            </div>
          ))}
        </nav>
      )}

      {currentFolderId !== null && (
        <button
          onClick={() => {
            const parent = breadcrumbs[breadcrumbs.length - 2];
            navigateToFolder(parent?.id ?? null);
          }}
          className="mb-4 inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-dark-300 transition-colors hover:bg-white/[0.05] hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          Back
        </button>
      )}

      {visibleKeys.length > 0 && (
        <div className="mb-4 flex min-w-0 flex-wrap items-center gap-2 overflow-hidden rounded-xl border border-primary-500/15 bg-primary-500/[0.06] p-2.5">
          <button
            onClick={selectAllVisible}
            className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-primary-100 transition-colors hover:bg-primary-500/10"
          >
            <CheckSquare className="h-4 w-4" />
            {visibleKeys.every((key) => selected.has(key))
              ? "Clear selection"
              : "Select visible"}
          </button>
          {selected.size > 0 && (
            <>
              <span className="text-sm text-dark-300">{selected.size} selected</span>
              <button
                onClick={() => restore()}
                disabled={restoreMutation.isPending}
                className="ml-auto inline-flex items-center gap-2 rounded-lg bg-primary-600 px-3 py-2 text-sm font-semibold text-white shadow-lg shadow-primary-500/20 transition-colors hover:bg-primary-500 disabled:opacity-50"
              >
                <RotateCcw className="h-4 w-4" />
                Restore
              </button>
              <button
                onClick={() => setConfirmAction("delete")}
                className="inline-flex items-center gap-2 rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm font-semibold text-red-300 transition-colors hover:bg-red-500/20"
              >
                <Trash2 className="h-4 w-4" />
                Delete forever
              </button>
            </>
          )}
        </div>
      )}

      {visibleKeys.length === 0 ? (
        <div className="flex min-h-[42vh] flex-col items-center justify-center rounded-2xl border border-dashed border-white/[0.08] bg-dark-900/30 p-8 text-center">
          <div className="mb-5 rounded-3xl border border-white/[0.05] bg-dark-800/60 p-6 text-dark-500">
            <Trash2 className="h-10 w-10" />
          </div>
          <h2 className="text-xl font-bold text-white">
            {searchQuery
              ? "No deleted items match your search"
              : currentFolderId === null
                ? "Recycle Bin is empty"
                : "This deleted folder is empty"}
          </h2>
          <p className="mt-2 max-w-md text-sm text-dark-400">
            Deleted files and folders remain read-only until restored or permanently deleted.
          </p>
        </div>
      ) : (
        <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {filtered.folders.map((item) => {
            const key = `folder:${item.id}` as SelectedKey;
            const checked = selected.has(key);
            return (
              <article
                key={key}
                className={`group min-w-0 overflow-hidden rounded-2xl border p-4 transition-all ${
                  checked
                    ? "border-primary-500/40 bg-primary-500/10 shadow-lg shadow-primary-500/5"
                    : "border-white/[0.06] bg-dark-900/55 hover:border-white/[0.12] hover:bg-dark-800/60"
                }`}
              >
                <div className="flex min-w-0 items-start gap-3">
                  <button
                    onClick={() => navigateToFolder(item.id)}
                    className="rounded-xl bg-primary-500/10 p-2.5 text-primary-300 transition-colors hover:bg-primary-500/20"
                    title={`Open ${item.name}`}
                  >
                    <Folder className="h-5 w-5" />
                  </button>
                  <button
                    onClick={() => navigateToFolder(item.id)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <h3 className="block w-full truncate font-semibold text-white" title={item.name}>
                      {item.name}
                    </h3>
                    <p className="mt-1 truncate text-xs text-dark-400">
                      Folder · {item.item_count} nested item{item.item_count === 1 ? "" : "s"}
                    </p>
                  </button>
                  <button
                    onClick={() => toggleSelected(key)}
                    aria-label={checked ? "Deselect folder" : "Select folder"}
                    className={`h-6 w-6 shrink-0 rounded-md border transition-colors ${
                      checked
                        ? "border-primary-400 bg-primary-500"
                        : "border-dark-600 bg-dark-800 hover:border-primary-400/50"
                    }`}
                  />
                </div>
                <div className="mt-4 flex min-w-0 flex-col gap-1 border-t border-white/[0.05] pt-3 text-xs sm:flex-row sm:items-center sm:justify-between sm:gap-2">
                  <span className="min-w-0 truncate text-orange-300">
                    {expiryLabel(item.purge_after)}
                  </span>
                  <span className="shrink-0 text-dark-500">{exactExpiry(item.purge_after)}</span>
                </div>
              </article>
            );
          })}

          {filtered.files.map((item) => {
            const key = `file:${item.id}` as SelectedKey;
            const checked = selected.has(key);
            const isActiveAudio =
              item.file_type === "audio" && activeAudioFileId === item.id;
            const isActiveVideo =
              item.file_type === "video" && activeVideoFileId === item.id;
            const isActiveMedia = isActiveAudio || isActiveVideo;
            const activeMediaIsPlaying = isActiveAudio
              ? activeAudioIsPlaying
              : activeVideoIsPlaying;
            const activeMediaLabel = isActiveAudio
              ? activeAudioIsPlaying
                ? "Now playing"
                : "Current audio"
              : activeVideoIsPlaying
                ? "Now playing"
                : "Current video";
            const resolutionLabel = appSettings.showVideoResolution
              ? getVideoResolutionLabel(item)
              : null;
            return (
              <article
                key={key}
                className={`group min-w-0 overflow-hidden rounded-2xl border p-4 transition-all ${
                  isActiveMedia
                    ? "border-primary-400/60 bg-primary-500/15 shadow-lg shadow-primary-500/20 ring-1 ring-primary-300/30"
                    : checked
                      ? "border-primary-500/40 bg-primary-500/10 shadow-lg shadow-primary-500/5"
                      : "border-white/[0.06] bg-dark-900/55 hover:border-white/[0.12] hover:bg-dark-800/60"
                }`}
              >
                <div className="flex min-w-0 items-start gap-3">
                  <TrashFileThumbnail
                    file={item}
                    onOpen={() => openFile(item)}
                    isActiveMedia={isActiveMedia}
                    activeMediaIsPlaying={activeMediaIsPlaying}
                  />
                  <button onClick={() => openFile(item)} className="min-w-0 flex-1 text-left">
                    <h3
                      className="block w-full truncate font-semibold text-white"
                      title={item.file_name}
                    >
                      {item.file_name}
                    </h3>
                    {isActiveMedia && (
                      <span className="mt-1 inline-flex items-center gap-1 rounded-full border border-primary-300/40 bg-primary-500/20 px-2 py-0.5 text-[10px] font-semibold text-primary-100">
                        <span
                          className={`h-1.5 w-1.5 rounded-full bg-primary-300 ${
                            activeMediaIsPlaying ? "animate-pulse" : ""
                          }`}
                        />
                        {activeMediaLabel}
                      </span>
                    )}
                    <p className="mt-1 truncate text-xs capitalize text-dark-400">
                      {item.file_type}
                      {resolutionLabel ? ` · ${resolutionLabel}` : ""}
                      {item.duration ? ` · ${formatDuration(item.duration)}` : ""}
                      {` · ${formatFileSize(item.file_size)}`}
                    </p>
                    <span className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary-300">
                      <Eye className="h-3.5 w-3.5" />
                      Open
                    </span>
                  </button>
                  <button
                    onClick={() => toggleSelected(key)}
                    aria-label={checked ? "Deselect file" : "Select file"}
                    className={`h-6 w-6 shrink-0 rounded-md border transition-colors ${
                      checked
                        ? "border-primary-400 bg-primary-500"
                        : "border-dark-600 bg-dark-800 hover:border-primary-400/50"
                    }`}
                  />
                </div>
                <div className="mt-4 flex min-w-0 flex-col gap-1 border-t border-white/[0.05] pt-3 text-xs sm:flex-row sm:items-center sm:justify-between sm:gap-2">
                  <span className="min-w-0 truncate text-orange-300">
                    {expiryLabel(item.purge_after)}
                  </span>
                  <span className="shrink-0 text-dark-500">{exactExpiry(item.purge_after)}</span>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {confirmAction && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center overflow-x-hidden bg-black/60 p-4 backdrop-blur-sm">
          <div className="w-full min-w-0 max-w-sm overflow-hidden rounded-2xl border border-white/10 bg-dark-900 shadow-2xl animate-scale-in">
            <div className="p-6">
              <div className="mb-4 flex items-start justify-between gap-3">
                <div className="rounded-full bg-red-500/10 p-3 text-red-400">
                  <AlertTriangle className="h-6 w-6" />
                </div>
                <button
                  onClick={() => setConfirmAction(null)}
                  className="rounded-lg p-1 text-dark-400 hover:bg-white/5 hover:text-white"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>
              <h3 className="text-xl font-bold text-white">Delete permanently?</h3>
              <p className="mt-2 text-sm leading-relaxed text-dark-400">
                {confirmAction === "empty"
                  ? "Every item in Recycle Bin"
                  : `${selected.size} selected item${selected.size === 1 ? "" : "s"}`} will be
                removed from Telegram storage. This cannot be undone.
              </p>
            </div>
            <div className="flex flex-col gap-3 border-t border-white/5 bg-dark-800/40 p-4 sm:flex-row">
              <button
                onClick={() => setConfirmAction(null)}
                className="flex-1 rounded-lg px-4 py-2 text-sm font-medium text-dark-200 hover:bg-white/5"
              >
                Cancel
              </button>
              <button
                onClick={permanentlyDelete}
                disabled={deleteMutation.isPending || emptyMutation.isPending}
                className="flex-1 rounded-lg bg-red-500 px-4 py-2 text-sm font-semibold text-white hover:bg-red-600 disabled:opacity-50"
              >
                {deleteMutation.isPending || emptyMutation.isPending
                  ? "Deleting..."
                  : "Delete forever"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
