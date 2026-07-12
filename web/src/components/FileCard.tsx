/**
 * FileCard component - displays a single file in grid or list view
 */
import { useRef, useState } from "react";
import {
  Play,
  MoreVertical,
  Film,
  Music,
  FileText,
  Image,
  ChevronRight,
  Folder as FolderIcon,
} from "lucide-react";
import {
  TelegramFile,
  formatFileSize,
  formatDuration,
  toApiUrl,
  isImageFile,
  isPreviewableFile,
  getVideoResolutionLabel,
  mediaCrossOrigin,
} from "../lib/api";
import { useAppStore } from "../lib/store";

interface FileCardProps {
  file: TelegramFile;
  viewMode: "grid" | "list";
  selected: boolean;
  onSelect: (multi: boolean) => void;
  onPlay: () => void;
  folderPath?: Array<{ id: number | null; name: string }>;
  onOpenFolder?: (folderId: number | null | undefined) => void;
  selectionMode?: boolean;
  priorityThumbnail?: boolean;
}

export default function FileCard({
  file,
  viewMode,
  selected,
  onSelect,
  onPlay,
  folderPath,
  onOpenFolder,
  selectionMode = false,
  priorityThumbnail = false,
}: FileCardProps) {
  const {
    activeContextMenu,
    setActiveContextMenu,
    activeAudioFileId,
    activeAudioIsPlaying,
    activeVideoFileId,
    activeVideoIsPlaying,
    appSettings,
  } = useAppStore();
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const longPressTimer = useRef<number | null>(null);
  const longPressTriggered = useRef(false);
  const suppressNextClick = useRef(false);
  const pointerStart = useRef({ x: 0, y: 0 });

  const clearLongPressTimer = () => {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  };

  const isInteractiveTarget = (target: EventTarget | null) => {
    return (
      target instanceof HTMLElement &&
      !!target.closest(
        "button, a, input, textarea, select, [data-no-long-press]",
      )
    );
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || isInteractiveTarget(e.target)) return;

    longPressTriggered.current = false;
    pointerStart.current = { x: e.clientX, y: e.clientY };
    clearLongPressTimer();

    longPressTimer.current = window.setTimeout(() => {
      longPressTriggered.current = true;
      suppressNextClick.current = true;
      setActiveContextMenu(null);
      onSelect(true);
      navigator.vibrate?.(18);
    }, 500);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const moved =
      Math.abs(e.clientX - pointerStart.current.x) > 10 ||
      Math.abs(e.clientY - pointerStart.current.y) > 10;
    if (moved) clearLongPressTimer();
  };

  const handlePointerUpOrCancel = (e?: React.PointerEvent<HTMLDivElement>) => {
    clearLongPressTimer();
    if (longPressTriggered.current) {
      e?.preventDefault();
      e?.stopPropagation();
    }
  };

  // Check if this file's context menu is active
  const showMenu =
    activeContextMenu?.type === "file" &&
    activeContextMenu?.item.id === file.id;

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();

    if (longPressTriggered.current || suppressNextClick.current) {
      return;
    }

    if (!selected) {
      onSelect(false);
    }
    setActiveContextMenu({
      type: "file",
      item: file,
      x: e.clientX,
      y: e.clientY,
    });
  };

  const consumeLongPressClick = (e: React.MouseEvent) => {
    if (longPressTriggered.current || suppressNextClick.current) {
      e.preventDefault();
      e.stopPropagation();
      longPressTriggered.current = false;
      suppressNextClick.current = false;
      return true;
    }
    return false;
  };

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();

    if (consumeLongPressClick(e)) return;

    // Normal tap on the file details/card selects the file. The thumbnail/preview
    // area has its own click handler that opens the file instead.
    onSelect(selectionMode || e.ctrlKey || e.metaKey || e.shiftKey);
  };

  const handlePreviewClick = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();

    if (consumeLongPressClick(e)) return;

    if (selectionMode) {
      onSelect(true);
      return;
    }

    onPlay();
  };

  const handleDoubleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (consumeLongPressClick(e)) return;
    if (selectionMode) {
      onSelect(true);
      return;
    }
    onPlay();
  };

  // Stream/thumbnail endpoints authenticate through HttpOnly cookies on web.
  const authorizedThumbnailUrl = file.thumbnail_url ? toApiUrl(file.thumbnail_url) : null;
  // Cards only request the thumbnail endpoint. The backend may create a small
  // cached WebP on demand, but the card never loads the original image stream.
  const previewThumbUrl = thumbnailFailed ? null : authorizedThumbnailUrl;
  const isActiveAudioFile =
    file.file_type === "audio" && activeAudioFileId === file.id;
  const isActiveVideoFile =
    file.file_type === "video" && activeVideoFileId === file.id;
  const isActiveMediaFile = isActiveAudioFile || isActiveVideoFile;
  const activeMediaIsPlaying = isActiveAudioFile
    ? activeAudioIsPlaying
    : activeVideoIsPlaying;
  const activeMediaLabel = isActiveAudioFile
    ? activeAudioIsPlaying
      ? "Now playing"
      : "Current audio"
    : activeVideoIsPlaying
      ? "Now playing"
      : "Current video";

  const displayFileType = isImageFile(file) ? "image" : file.file_type;
  const videoResolutionLabel = appSettings.showVideoResolution
    ? getVideoResolutionLabel(file)
    : null;
  const shouldShowFolderPath =
    !!folderPath && folderPath.length > 1 && !!onOpenFolder;

  const FolderPath = ({ compact = false }: { compact?: boolean }) => {
    if (!shouldShowFolderPath) return null;

    return (
      <div
        className={`flex items-center gap-0.5 overflow-x-auto ${compact ? "mt-1" : "mt-1.5"} text-[10px] text-dark-500 no-scrollbar`}
      >
        <FolderIcon className="w-3 h-3 shrink-0 text-dark-500" />
        {folderPath!.map((crumb, index) => (
          <div
            key={`${crumb.id ?? "root"}-${index}`}
            className="flex items-center shrink-0 min-w-0"
          >
            {index > 0 && (
              <ChevronRight className="w-3 h-3 text-dark-600 mx-0.5 shrink-0" />
            )}
            <button
              onClick={(e) => {
                e.stopPropagation();
                onOpenFolder?.(crumb.id);
              }}
              title={crumb.name}
              className={`truncate hover:text-primary-300 hover:underline ${compact ? "max-w-[70px]" : "max-w-[110px]"}`}
            >
              {index === 0 ? "Home" : crumb.name}
            </button>
          </div>
        ))}
      </div>
    );
  };

  const getIcon = () => {
    switch (displayFileType) {
      case "video":
        return <Film className="w-8 h-8 text-primary-400" />;
      case "audio":
        return <Music className="w-8 h-8 text-pink-400" />;
      case "image":
        return <Image className="w-8 h-8 text-emerald-400" />;
      default:
        return <FileText className="w-8 h-8 text-blue-400" />;
    }
  };

  const getSmallIcon = () => {
    switch (displayFileType) {
      case "video":
        return <Film className="w-3 h-3 text-primary-400" />;
      case "audio":
        return <Music className="w-3 h-3 text-pink-400" />;
      case "image":
        return <Image className="w-3 h-3 text-emerald-400" />;
      default:
        return <FileText className="w-3 h-3 text-blue-400" />;
    }
  };

  if (viewMode === "list") {
    return (
      <div
        className={`flex items-center gap-4 p-3 rounded-xl cursor-pointer transition-all duration-200 animate-slide-up active:scale-[0.99]
                    ${
                      isActiveMediaFile
                        ? "bg-primary-500/15 border border-primary-400/60 shadow-lg shadow-primary-500/20 ring-1 ring-primary-300/30"
                        : selected
                          ? "bg-primary-500/10 border border-primary-500/30"
                          : "glass-card hover:bg-white/[0.03] border-white/[0.05]"
                    }`}
        onClick={handleClick}
        onContextMenu={handleContextMenu}
        onDoubleClick={handleDoubleClick}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUpOrCancel}
        onPointerCancel={handlePointerUpOrCancel}
        onPointerLeave={handlePointerUpOrCancel}
        data-file-id={file.id}
      >
        <div
          className="w-12 h-12 rounded-lg bg-dark-800/80 flex items-center justify-center overflow-hidden shrink-0 border border-white/[0.05]"
          onClick={handlePreviewClick}
          title="Open file"
        >
          {previewThumbUrl ? (
            <img
              src={previewThumbUrl}
              alt={file.file_name}
              crossOrigin={mediaCrossOrigin}
              className="w-full h-full object-cover"
              loading={priorityThumbnail ? "eager" : "lazy"}
              decoding={priorityThumbnail ? "auto" : "async"}
              draggable={false}
              onError={() => setThumbnailFailed(true)}
            />
          ) : (
            getIcon()
          )}
        </div>

        <div className="flex-1 min-w-0">
          <p
            className={`font-medium truncate text-sm ${selected ? "text-primary-200" : "text-white"}`}
          >
            {file.file_name}
          </p>
          <div className="flex items-center gap-3 text-xs text-dark-400 mt-1">
            {isActiveMediaFile && (
              <span className="inline-flex items-center gap-1 rounded-full border border-primary-300/40 bg-primary-500/20 px-2 py-0.5 text-[10px] font-semibold text-primary-100 shadow-sm shadow-primary-500/20">
                <span
                  className={`h-1.5 w-1.5 rounded-full bg-primary-300 ${activeMediaIsPlaying ? "animate-pulse" : ""}`}
                />
                {activeMediaLabel}
              </span>
            )}
            <span className="flex items-center gap-1">
              {getSmallIcon()}
              <span className="capitalize">{displayFileType}</span>
            </span>
            {videoResolutionLabel && (
              <>
                <span className="w-1 h-1 rounded-full bg-dark-600"></span>
                <span>{videoResolutionLabel}</span>
              </>
            )}
            <span className="w-1 h-1 rounded-full bg-dark-600"></span>
            <span>{formatFileSize(file.file_size)}</span>
            {file.duration && (
              <>
                <span className="w-1 h-1 rounded-full bg-dark-600"></span>
                <span>{formatDuration(file.duration)}</span>
              </>
            )}
            {file.last_pos &&
              file.duration &&
              file.last_pos / file.duration > 0.05 && (
                <>
                  <span className="w-1 h-1 rounded-full bg-dark-600"></span>
                  <span className="text-primary-400">
                    {Math.round((file.last_pos / file.duration) * 100)}%
                  </span>
                </>
              )}
          </div>
          <FolderPath />
        </div>

        <div className="relative">
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (showMenu) {
                setActiveContextMenu(null);
              } else {
                const rect = e.currentTarget.getBoundingClientRect();
                setActiveContextMenu({
                  type: "file",
                  item: file,
                  x: rect.right,
                  y: rect.bottom,
                });
              }
            }}
            className={`p-2 rounded-lg transition-colors ${showMenu ? "bg-white/10 text-white" : "hover:bg-white/[0.08] text-dark-400"}`}
          >
            <MoreVertical className="w-4 h-4" />
          </button>
        </div>
      </div>
    );
  }

  // Grid view
  return (
    <div
      className={`p-3 rounded-xl cursor-pointer transition-all duration-300 group relative animate-scale-in select-none
                ${
                  isActiveMediaFile
                    ? "bg-primary-500/15 border border-primary-400/60 shadow-xl shadow-primary-500/20 ring-1 ring-primary-300/30"
                    : selected
                      ? "bg-primary-500/10 border border-primary-500/30 shadow-lg shadow-primary-500/5"
                      : "glass-card hover:bg-dark-800/60 hover:shadow-xl hover:shadow-black/20 hover:-translate-y-1"
                }`}
      onClick={handleClick}
      onContextMenu={handleContextMenu}
      onDoubleClick={handleDoubleClick}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUpOrCancel}
      onPointerCancel={handlePointerUpOrCancel}
      onPointerLeave={handlePointerUpOrCancel}
      data-file-id={file.id}
    >
      <div
        className={`aspect-video rounded-lg mb-3 overflow-hidden relative border ${isActiveMediaFile ? "border-primary-300/60 shadow-inner shadow-primary-500/20" : selected ? "border-primary-500/20" : "border-white/[0.05]"} bg-dark-900/50`}
        onClick={handlePreviewClick}
        title="Open file"
      >
        {previewThumbUrl ? (
          <>
            <img
              src={previewThumbUrl}
              alt={file.file_name}
              crossOrigin={mediaCrossOrigin}
              className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
              loading={priorityThumbnail ? "eager" : "lazy"}
              decoding={priorityThumbnail ? "auto" : "async"}
              draggable={false}
              onError={() => setThumbnailFailed(true)}
            />
            {/* Gradient overlay */}
            <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300"></div>
          </>
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            {getIcon()}
          </div>
        )}

        {/* Progress Bar */}
        {file.last_pos &&
          file.duration &&
          file.last_pos / file.duration > 0.05 && (
            <div className="absolute bottom-0 left-0 right-0 h-1 bg-black/40">
              <div
                className="h-full bg-primary-500"
                style={{
                  width: `${Math.min(100, (file.last_pos / file.duration) * 100)}%`,
                }}
              />
            </div>
          )}

        {isActiveMediaFile && (
          <div className="absolute left-1.5 top-1.5 inline-flex items-center gap-1 rounded-full border border-primary-300/40 bg-black/55 px-2 py-0.5 text-[10px] font-semibold text-primary-100 shadow-lg shadow-primary-500/20 backdrop-blur-md">
            <span
              className={`h-1.5 w-1.5 rounded-full bg-primary-300 ${activeMediaIsPlaying ? "animate-pulse" : ""}`}
            />
            {activeMediaLabel}
          </div>
        )}

        {/* Resolution badge */}
        {videoResolutionLabel && (
          <div className="absolute left-1.5 bottom-1.5 bg-black/60 backdrop-blur-md px-1.5 py-0.5 rounded text-[10px] font-medium text-white shadow-sm">
            {videoResolutionLabel}
          </div>
        )}

        {/* Duration badge */}
        {file.duration && (
          <div className="absolute bottom-1.5 right-1.5 bg-black/60 backdrop-blur-md px-1.5 py-0.5 rounded text-[10px] font-medium text-white shadow-sm">
            {formatDuration(file.duration)}
          </div>
        )}

        {/* Preview overlay for playable media/images */}
        {isPreviewableFile(file) && (
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-all duration-300">
            <div className="w-10 h-10 rounded-full bg-white/10 backdrop-blur-md flex items-center justify-center shadow-lg border border-white/20 hover:scale-110 transition-transform">
              {isImageFile(file) ? (
                <Image className="w-4 h-4 text-white" />
              ) : (
                <Play
                  className="w-4 h-4 text-white ml-0.5"
                  fill="currentColor"
                />
              )}
            </div>
          </div>
        )}
      </div>

      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p
            className={`font-medium text-sm truncate transition-colors ${selected ? "text-primary-200" : "text-white group-hover:text-primary-300"}`}
            title={file.file_name}
          >
            {file.file_name}
          </p>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            {isActiveMediaFile && (
              <span className="inline-flex items-center gap-1 rounded-md border border-primary-300/40 bg-primary-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-primary-100">
                <span
                  className={`h-1.5 w-1.5 rounded-full bg-primary-300 ${activeMediaIsPlaying ? "animate-pulse" : ""}`}
                />
                {activeMediaLabel}
              </span>
            )}
            <span
              className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-md border ${
                selected
                  ? "bg-primary-500/20 border-primary-500/20 text-primary-300"
                  : "bg-dark-800 border-white/[0.05] text-dark-400 group-hover:border-white/[0.1]"
              }`}
            >
              {getSmallIcon()}
              <span className="capitalize">{displayFileType}</span>
            </span>
            {videoResolutionLabel && (
              <span className="rounded-md border border-white/[0.05] bg-dark-800 px-1.5 py-0.5 text-[10px] text-dark-300 group-hover:border-white/[0.1]">
                {videoResolutionLabel}
              </span>
            )}
            <p className="text-[10px] text-dark-500">
              {formatFileSize(file.file_size)}
            </p>
          </div>
          <FolderPath compact />
        </div>

        <div
          className={`${showMenu ? "opacity-100" : "opacity-0 group-hover:opacity-100"} transition-opacity`}
        >
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (showMenu) {
                setActiveContextMenu(null);
              } else {
                const rect = e.currentTarget.getBoundingClientRect();
                setActiveContextMenu({
                  type: "file",
                  item: file,
                  x: rect.right,
                  y: rect.bottom,
                });
              }
            }}
            className={`p-1.5 rounded-lg transition-colors ${showMenu ? "bg-white/10 text-white" : "hover:bg-white/[0.08] text-dark-400"}`}
          >
            <MoreVertical className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
