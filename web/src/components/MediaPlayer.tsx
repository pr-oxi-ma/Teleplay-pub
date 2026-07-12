/**
 * MediaPlayer - full screen video/audio/image preview player
 */
import { useRef, useEffect, useState, useCallback, useMemo } from "react";
import {
  X,
  Play,
  Pause,
  Volume2,
  VolumeX,
  Maximize,
  Minimize,
  SkipBack,
  SkipForward,
  Download,
  ExternalLink,
  AlertTriangle,
  Copy,
  PictureInPicture2,
  Gauge,
  ChevronDown,
  ChevronUp,
  Image as ImageIcon,
} from "lucide-react";
import {
  api,
  TelegramFile,
  formatDuration,
  useUpdateProgress,
  useFile,
  toApiUrl,
  isImageFile,
  isTimedMediaFile,
  mediaCrossOrigin,
} from "../lib/api";
import { PlaybackMode, useAppStore } from "../lib/store";

interface PlaybackModeOption {
  value: PlaybackMode;
  label: string;
  shortLabel: string;
  icon: string;
  description: string;
}

// Easy-to-edit delay before showing the unsupported/playback-error popup.
// Use ms or s only: "100ms" for fast servers, "5s" for slow servers.
const PLAYBACK_ERROR_DELAY_TIME = "3s";
const MEDIA_SWITCH_THROTTLE_MS = 180;
// Coalesce rapid programmatic seeks (10s buttons / keyboard repeat / double taps)
// into one real currentTime write. This prevents Android/Chromium streams from
// painting fast-forward/rewind intermediate frames and reduces HTTP Range spam.
const SEEK_COMMIT_DEBOUNCE_MS = 90;

// Startup retry prevents slow Telegram/server startup from becoming a false
// "Playback not supported" popup. It retries the same stream with a cache-bust
// query before showing any hard error.
const MEDIA_STARTUP_MAX_RETRIES = 4;

const parseDelayTime = (value: string): number => {
  const match = value.trim().toLowerCase().match(/^(\d+(?:\.\d+)?)(ms|s)$/);

  if (!match) {
    console.warn(
      `Invalid PLAYBACK_ERROR_DELAY_TIME: ${value}. Use values like "100ms" or "5s". Falling back to 3s.`,
    );
    return 3000;
  }

  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount < 0) return 3000;

  return match[2] === "s" ? amount * 1000 : amount;
};

const PLAYBACK_ERROR_DELAY_MS = parseDelayTime(PLAYBACK_ERROR_DELAY_TIME);
const MEDIA_STARTUP_RETRY_DELAY_MS = Math.max(250, Math.min(1200, PLAYBACK_ERROR_DELAY_MS / 3));

const MEDIA_ERR_ABORTED = 1;
const MEDIA_ERR_NETWORK = 2;
const MEDIA_ERR_DECODE = 3;
const MEDIA_ERR_SRC_NOT_SUPPORTED = 4;

const PLAYER_ERROR_MESSAGES = {
  unsupportedImage:
    "Browser cannot preview this image format. You can download or open the file directly.",
  unsupportedMedia: "Playback is not supported for this media in your browser.",
  genericMedia: "An error occurred while trying to play this media.",
};

const abortMediaElementLoad = (element: HTMLMediaElement | null) => {
  if (!element) return;

  try {
    element.pause();
  } catch {
    // Ignore browser-specific media abort errors.
  }

  try {
    element.removeAttribute("src");
    element.load();
  } catch {
    // Removing src/load() is best-effort cleanup for fast media switching.
  }
};

const PLAYBACK_MODE_OPTIONS: PlaybackModeOption[] = [
  {
    value: "normal",
    label: "Normal",
    shortLabel: "Normal",
    icon: "→",
    description: "End → next, last → first",
  },
  {
    value: "repeat_one",
    label: "Repeat One",
    shortLabel: "Repeat 1",
    icon: "🔂",
    description: "Repeat the current video/audio",
  },
  {
    value: "shuffle",
    label: "Shuffle",
    shortLabel: "Shuffle",
    icon: "🔀",
    description: "Play a random video/audio next",
  },
  {
    value: "autoplay_off",
    label: "Autoplay Off",
    shortLabel: "Auto Off",
    icon: "⏸",
    description: "Stop when the current video/audio ends",
  },
  {
    value: "stop_after_this",
    label: "Stop After This",
    shortLabel: "Stop",
    icon: "⏹",
    description: "Stop after this video/audio, then return to Normal",
  },
];

export default function MediaPlayer() {
  const {
    previewFile: file,
    setPreviewFile,
    setImageViewerFile,
    isPlayerMinimized,
    setPlayerMinimized,
  } = useAppStore();

  const timedFile = file && !isImageFile(file) ? file : null;

  // Backward compatibility: if an image is ever placed in previewFile,
  // move it into the separate image viewer so audio/music keeps playing.
  useEffect(() => {
    if (file && isImageFile(file)) {
      setImageViewerFile(file);
      setPreviewFile(null);
    }
  }, [file, setImageViewerFile, setPreviewFile]);

  return (
    <>
      {timedFile && (
        <MediaPlayerContent
          file={timedFile}
          onClose={() => setPreviewFile(null)}
          isMinimized={isPlayerMinimized}
          setMinimized={setPlayerMinimized}
        />
      )}
      <ImageViewer />
    </>
  );
}

interface MediaPlayerContentProps {
  file: TelegramFile;
  onClose: () => void;
  isMinimized: boolean;
  setMinimized: (minimized: boolean) => void;
}

function MediaPlayerContent({
  file,
  onClose,
  isMinimized,
  setMinimized,
}: MediaPlayerContentProps) {
  const isTrashFile = Boolean(file.deleted_at);
  const {
    mediaQueue,
    setPreviewFile,
    setActiveAudioPlayback,
    setActiveVideoPlayback,
    appSettings,
    updateAppSettings,
  } = useAppStore();
  const videoRef = useRef<HTMLMediaElement | null>(null);
  const mediaElementRef = useRef<HTMLMediaElement | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [seekPreviewTime, setSeekPreviewTime] = useState<number | null>(null);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [isMuted, setIsMuted] = useState(false);
  const [showControls, setShowControls] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(
    appSettings.defaultPlaybackSpeed,
  );
  const [playbackMode, setPlaybackMode] = useState<PlaybackMode>(
    appSettings.playbackMode,
  );
  const [isPiP, setIsPiP] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [streamRetryNonce, setStreamRetryNonce] = useState(0);
  const [usingFallbackStream, setUsingFallbackStream] = useState(false);
  const [, setIsSeekingMedia] = useState(false);
  const hideControlsTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const delayedErrorTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const startupRetryTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const delayedErrorFileId = useRef<number | null>(null);
  const delayedErrorStreamUrl = useRef<string | null>(null);
  const mediaLoadToken = useRef(0);
  const lastMediaSwitchAt = useRef(0);
  const neighborPrefetchTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const [publicUrl, setPublicUrl] = useState<string | null>(null);
  const [seekFeedback, setSeekFeedback] = useState<{
    direction: "back" | "forward";
    key: number;
  } | null>(null);
  const [isHoldSpeedActive, setIsHoldSpeedActive] = useState(false);
  const lastTapRef = useRef<{ time: number; side: "left" | "right" } | null>(
    null,
  );
  const singleTapTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seekFeedbackTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const seekSettledTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const seekSpinnerTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const pendingSeekTargetRef = useRef<number | null>(null);
  const pendingSeekCommitTimeout = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const pendingSeekResumeOverrideRef = useRef<boolean | undefined>(undefined);
  const isScrubbingSeekRef = useRef(false);
  const seekRequestIdRef = useRef(0);
  const resumeAfterSeekRef = useRef(false);
  const suppressProgrammaticSeekPauseRef = useRef(false);
  const holdSpeedTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const holdSpeedRestoreRate = useRef(1);
  const holdSpeedActiveRef = useRef(false);
  const suppressNextSurfaceTap = useRef(false);
  const gesturePointerId = useRef<number | null>(null);
  const gesturePointerStart = useRef({ x: 0, y: 0 });
  const restoredProgressFileId = useRef<number | null>(null);
  const userChangedPositionRef = useRef(false);
  const clearedProgressFileId = useRef<number | null>(null);
  const mediaLoadedMetadataRef = useRef(false);
  const mediaHasPlayableDataRef = useRef(false);
  const startupRetryCountRef = useRef(0);

  // Fetch fresh file details to get latest progress
  const { data: extendedFile } = useFile(file.id, !isTrashFile);
  const { mutate: updateProgress } = useUpdateProgress();

  const isVideo = file.file_type === "video";
  const isAudio = file.file_type === "audio";
  const isImage = isImageFile(file);
  const isTimedMedia = isVideo || isAudio;
  const previewQueueFromStore = useMemo(
    () => mediaQueue.filter(isTimedMediaFile),
    [mediaQueue],
  );
  const previewQueue = useMemo(
    () =>
      isTrashFile
        ? [file]
        : previewQueueFromStore.some((item) => item.id === file.id)
          ? previewQueueFromStore
          : [file, ...previewQueueFromStore],
    [file, isTrashFile, previewQueueFromStore],
  );
  const currentMediaIndex = previewQueue.findIndex(
    (item) => item.id === file.id,
  );
  const hasMultipleMedia = previewQueue.length > 1 && currentMediaIndex !== -1;
  const currentPlaybackMode =
    PLAYBACK_MODE_OPTIONS.find((option) => option.value === playbackMode) ||
    PLAYBACK_MODE_OPTIONS[0];
  useEffect(() => {
    if (!isAudio) return;

    setActiveAudioPlayback(file.id, false);

    return () => {
      setActiveAudioPlayback(null, false);
    };
  }, [file.id, isAudio, setActiveAudioPlayback]);

  useEffect(() => {
    if (isAudio) {
      setActiveAudioPlayback(file.id, isPlaying);
    }
  }, [file.id, isAudio, isPlaying, setActiveAudioPlayback]);

  useEffect(() => {
    if (!isVideo) return;

    setActiveVideoPlayback(file.id, false);

    return () => {
      setActiveVideoPlayback(null, false);
    };
  }, [file.id, isVideo, setActiveVideoPlayback]);

  useEffect(() => {
    if (isVideo) {
      setActiveVideoPlayback(file.id, isPlaying);
    }
  }, [file.id, isPlaying, isVideo, setActiveVideoPlayback]);

  useEffect(() => {
    setPlaybackMode(appSettings.playbackMode);
  }, [appSettings.playbackMode]);

  useEffect(() => {
    setPlaybackSpeed(appSettings.defaultPlaybackSpeed);
    if (videoRef.current && !holdSpeedActiveRef.current) {
      videoRef.current.playbackRate = appSettings.defaultPlaybackSpeed;
    }
  }, [appSettings.defaultPlaybackSpeed]);

  const applyPlaybackMode = useCallback(
    (mode: PlaybackMode) => {
      setPlaybackMode(mode);
      updateAppSettings({ playbackMode: mode });
    },
    [updateAppSettings],
  );

  const cyclePlaybackMode = useCallback(
    (e?: React.MouseEvent<HTMLButtonElement>) => {
      e?.stopPropagation();

      const currentIndex = PLAYBACK_MODE_OPTIONS.findIndex(
        (option) => option.value === playbackMode,
      );
      const safeIndex = currentIndex === -1 ? 0 : currentIndex;
      applyPlaybackMode(
        PLAYBACK_MODE_OPTIONS[(safeIndex + 1) % PLAYBACK_MODE_OPTIONS.length]
          .value,
      );
    },
    [applyPlaybackMode, playbackMode],
  );

  const clearDelayedError = useCallback(() => {
    if (delayedErrorTimeout.current) {
      clearTimeout(delayedErrorTimeout.current);
      delayedErrorTimeout.current = null;
    }
    delayedErrorFileId.current = null;
    delayedErrorStreamUrl.current = null;
  }, []);

  const clearStartupRetry = useCallback(() => {
    if (startupRetryTimeout.current) {
      clearTimeout(startupRetryTimeout.current);
      startupRetryTimeout.current = null;
    }
  }, []);

  const setMediaElementRef = useCallback((element: HTMLVideoElement | HTMLAudioElement | null) => {
    const previousElement = mediaElementRef.current;

    if (previousElement && previousElement !== element) {
      abortMediaElementLoad(previousElement);
    }

    mediaElementRef.current = element;
    videoRef.current = element as HTMLVideoElement | null;
  }, []);

  const abortCurrentMediaLoad = useCallback(() => {
    abortMediaElementLoad(mediaElementRef.current);
  }, []);

  const showErrorAfterDelay = useCallback(
    (message: string, streamUrl: string, loadToken: number) => {
      clearDelayedError();

      const errorFileId = file.id;
      delayedErrorFileId.current = errorFileId;
      delayedErrorStreamUrl.current = streamUrl;
      setIsLoading(true);

      delayedErrorTimeout.current = setTimeout(() => {
        const mediaElement = videoRef.current;
        const currentSrc = mediaElement?.currentSrc || mediaElement?.src || "";

        if (delayedErrorFileId.current !== errorFileId) return;
        if (delayedErrorStreamUrl.current !== streamUrl) return;
        if (mediaLoadToken.current !== loadToken) return;
        if (currentSrc && currentSrc !== streamUrl) return;

        if (!mediaElement) {
          clearDelayedError();
          return;
        }

        // If the selected media loaded enough to render a frame, the old error is stale.
        if (mediaElement.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
          clearDelayedError();
          setIsLoading(false);
          return;
        }

        // Slow Telegram/server startup is not an unsupported-file error. When the
        // browser is still fetching, keep the loading UI and do not show the popup.
        if (mediaElement.networkState === HTMLMediaElement.NETWORK_LOADING) {
          delayedErrorTimeout.current = null;
          delayedErrorFileId.current = null;
          delayedErrorStreamUrl.current = null;
          setIsLoading(true);
          return;
        }

        const mediaError = mediaElement.error;
        if (!mediaError || mediaError.code === MEDIA_ERR_ABORTED) {
          clearDelayedError();
          setIsLoading(true);
          return;
        }

        // No metadata/frame yet means this is still a startup/load failure.
        // Do not show "playback not supported" for slow Telegram/server startup.
        if (!mediaLoadedMetadataRef.current && mediaElement.readyState < HTMLMediaElement.HAVE_METADATA) {
          clearDelayedError();
          setIsLoading(true);
          return;
        }

        setError(message);
        setIsLoading(false);
        delayedErrorTimeout.current = null;
      }, PLAYBACK_ERROR_DELAY_MS);
    },
    [clearDelayedError, file.id],
  );

  // Reset player UI state whenever a new file is selected.
  // Also invalidate any old <video>/<audio> events from the previous file/session.
  useEffect(() => {
    mediaLoadToken.current += 1;
    clearDelayedError();
    clearStartupRetry();
    setCurrentTime(0);
    setDuration(0);
    setIsPlaying(false);
    setIsLoading(true);
    setError(null);
    setStreamRetryNonce(0);
    setIsSeekingMedia(false);
    setSeekPreviewTime(null);
    pendingSeekTargetRef.current = null;
    isScrubbingSeekRef.current = false;
    seekRequestIdRef.current += 1;
    resumeAfterSeekRef.current = false;
    suppressProgrammaticSeekPauseRef.current = false;
    if (seekSpinnerTimeout.current) {
      clearTimeout(seekSpinnerTimeout.current);
      seekSpinnerTimeout.current = null;
    }
    if (seekSettledTimeout.current) {
      clearTimeout(seekSettledTimeout.current);
      seekSettledTimeout.current = null;
    }
    setPublicUrl(null);
    restoredProgressFileId.current = null;
    userChangedPositionRef.current = false;
    clearedProgressFileId.current = null;
    mediaLoadedMetadataRef.current = false;
    mediaHasPlayableDataRef.current = false;
    startupRetryCountRef.current = 0;
    setUsingFallbackStream(false);

    return () => {
      mediaLoadToken.current += 1;
      clearDelayedError();
      clearStartupRetry();
    };
  }, [clearDelayedError, clearStartupRetry, file.id]);

  useEffect(() => {
    return () => {
      clearDelayedError();
      clearStartupRetry();
    };
  }, [clearDelayedError, clearStartupRetry]);

  useEffect(() => {
    return () => {
      if (singleTapTimeout.current) clearTimeout(singleTapTimeout.current);
      if (seekFeedbackTimeout.current)
        clearTimeout(seekFeedbackTimeout.current);
      if (seekSettledTimeout.current) {
        clearTimeout(seekSettledTimeout.current);
        seekSettledTimeout.current = null;
      }
      if (seekSpinnerTimeout.current) {
        clearTimeout(seekSpinnerTimeout.current);
        seekSpinnerTimeout.current = null;
      }
      if (pendingSeekCommitTimeout.current) {
        clearTimeout(pendingSeekCommitTimeout.current);
        pendingSeekCommitTimeout.current = null;
      }
      if (holdSpeedTimeout.current) clearTimeout(holdSpeedTimeout.current);
      if (neighborPrefetchTimeout.current) {
        clearTimeout(neighborPrefetchTimeout.current);
        neighborPrefetchTimeout.current = null;
      }
      if (startupRetryTimeout.current) {
        clearTimeout(startupRetryTimeout.current);
        startupRetryTimeout.current = null;
      }
      if (holdSpeedActiveRef.current && videoRef.current) {
        videoRef.current.playbackRate = holdSpeedRestoreRate.current || 1;
      }
    };
  }, []);

  const getAbsoluteUrl = (url: string) => toApiUrl(url);

  const selectedStreamUrl =
    usingFallbackStream && file.fallback_stream_url
      ? file.fallback_stream_url
      : file.stream_url;
  const streamRetryQuery = streamRetryNonce > 0
    ? `${selectedStreamUrl.includes("?") ? "&" : "?"}_retry=${streamRetryNonce}`
    : "";
  const relativeStreamUrl = `${selectedStreamUrl}${streamRetryQuery}`;
  const authorizedStreamUrl = getAbsoluteUrl(relativeStreamUrl);
  const externalUrl = publicUrl || authorizedStreamUrl;
  const vlcUrl = `vlc://${externalUrl}`;

  // Authorized Thumbnail URL
  const relativeThumbnailUrl = file.thumbnail_url || null;
  const authorizedThumbnailUrl = relativeThumbnailUrl
    ? getAbsoluteUrl(relativeThumbnailUrl)
    : null;
  const minimizedThumbUrl =
    authorizedThumbnailUrl || (isImage ? authorizedStreamUrl : null);

  const isCurrentMediaEvent = useCallback(
    (event?: React.SyntheticEvent<HTMLMediaElement>) => {
      const target = event?.currentTarget;
      if (!target) return true;

      if (target !== mediaElementRef.current) {
        return false;
      }

      const currentSrc = target.currentSrc || target.getAttribute("src") || "";
      return !currentSrc || currentSrc === authorizedStreamUrl;
    },
    [authorizedStreamUrl],
  );

  // Do not auto-create a public share link on every playback. That extra DB/write
  // request made opening media slower and was unnecessary because the authorized
  // stream URL already works with the token query param. Existing public links
  // are still used when they are already present.
  useEffect(() => {
    if (isTrashFile) {
      setPublicUrl(null);
      return;
    }

    const fileData = extendedFile || file;
    setPublicUrl(
      fileData.public_stream_url
        ? getAbsoluteUrl(fileData.public_stream_url)
        : null,
    );
  }, [extendedFile, file, isTrashFile]);

  const restoreSavedProgress = useCallback(() => {
    const mediaElement = videoRef.current;
    if (!appSettings.resumePlayback) {
      restoredProgressFileId.current = file.id;
      return;
    }
    if (!isTimedMedia || !mediaElement || mediaElement.readyState < 1) return;
    if (restoredProgressFileId.current === file.id) return;
    if (userChangedPositionRef.current) return;

    const savedPosition = Math.floor(
      extendedFile?.last_pos ?? file.last_pos ?? 0,
    );

    if (savedPosition <= 0) {
      restoredProgressFileId.current = file.id;
      return;
    }

    const mediaDuration = Number.isFinite(mediaElement.duration)
      ? mediaElement.duration
      : extendedFile?.duration || file.duration || 0;

    if (mediaDuration > 0 && savedPosition >= mediaDuration * 0.95) {
      restoredProgressFileId.current = file.id;
      clearedProgressFileId.current = file.id;
      return;
    }

    mediaElement.currentTime = savedPosition;
    setCurrentTime(savedPosition);
    restoredProgressFileId.current = file.id;
  }, [
    extendedFile?.duration,
    extendedFile?.last_pos,
    file.duration,
    file.id,
    file.last_pos,
    isTimedMedia,
    appSettings.resumePlayback,
  ]);

  // Restore saved progress only once per opened audio/video file.
  useEffect(() => {
    restoreSavedProgress();
  }, [restoreSavedProgress]);

  // Save progress periodically only for audio/video
  useEffect(() => {
    const interval = setInterval(() => {
      if (
        appSettings.resumePlayback &&
        isTimedMedia &&
        isPlaying &&
        videoRef.current &&
        !error
      ) {
        updateProgress({
          fileId: file.id,
          position: Math.floor(videoRef.current.currentTime),
          duration: videoRef.current.duration,
        });
      }
    }, 10000); // Save every 10s

    return () => clearInterval(interval);
  }, [
    appSettings.resumePlayback,
    isTimedMedia,
    isPlaying,
    file.id,
    error,
    updateProgress,
  ]);

  // Save on close/pause
  const saveProgress = useCallback(
    (positionOverride?: number) => {
      if (
        appSettings.resumePlayback &&
        isTimedMedia &&
        videoRef.current &&
        !error
      ) {
        if (
          positionOverride === undefined &&
          clearedProgressFileId.current === file.id
        ) {
          return;
        }

        const mediaDuration = Number.isFinite(videoRef.current.duration)
          ? videoRef.current.duration
          : duration || file.duration || 0;
        const position = Math.max(
          0,
          Math.floor(positionOverride ?? videoRef.current.currentTime),
        );

        if (position <= 0) {
          clearedProgressFileId.current = file.id;
        } else {
          clearedProgressFileId.current = null;
        }

        updateProgress({
          fileId: file.id,
          position,
          duration: mediaDuration,
        });
      }
    },
    [
      appSettings.resumePlayback,
      isTimedMedia,
      file.id,
      file.duration,
      duration,
      error,
      updateProgress,
    ],
  );

  // Save on unmount
  useEffect(() => {
    return () => saveProgress();
  }, [saveProgress]);

  const togglePlay = useCallback((e?: any) => {
    e?.stopPropagation();
    if (!isTimedMedia || !videoRef.current) return;

    if (isPlaying) {
      videoRef.current.pause();
      saveProgress();
    } else {
      videoRef.current.play();
    }
    setIsPlaying(!isPlaying);
  }, [isPlaying, isTimedMedia, saveProgress]);

  const toggleMute = useCallback(() => {
    if (videoRef.current) {
      videoRef.current.muted = !isMuted;
      setIsMuted(!isMuted);
    }
  }, [isMuted]);

  const toggleFullscreen = async () => {
    try {
      if (!document.fullscreenElement) {
        await containerRef.current?.requestFullscreen();
        setIsFullscreen(true);
      } else {
        await document.exitFullscreen();
        setIsFullscreen(false);
      }
    } catch {
      // Fullscreen can be rejected by the browser if the gesture is interrupted.
      setIsFullscreen(document.fullscreenElement === containerRef.current);
    }
  };

  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(document.fullscreenElement === containerRef.current);
    };

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () =>
      document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  const exitFullscreenIfActive = useCallback(async () => {
    if (!document.fullscreenElement) {
      setIsFullscreen(false);
      return;
    }

    try {
      await document.exitFullscreen();
    } catch {
      // Best effort: the fullscreenchange listener will sync the final state.
    } finally {
      setIsFullscreen(document.fullscreenElement === containerRef.current);
    }
  }, []);

  const togglePiP = async () => {
    if (!isVideo) return;

    if (document.pictureInPictureElement) {
      await document.exitPictureInPicture();
      setIsPiP(false);
    } else if (videoRef.current instanceof HTMLVideoElement) {
      await videoRef.current.requestPictureInPicture();
      setIsPiP(true);
    }
  };

  const cycleSpeed = () => {
    if (!isTimedMedia) return;

    const speeds = [0.5, 1, 1.25, 1.5, 2];
    const currentIndex = speeds.indexOf(playbackSpeed);
    const nextSpeed = speeds[(currentIndex + 1) % speeds.length];
    setPlaybackSpeed(nextSpeed);
    updateAppSettings({ defaultPlaybackSpeed: nextSpeed });
    if (videoRef.current) {
      videoRef.current.playbackRate = nextSpeed;
    }
  };

  const clearSingleTapTimer = useCallback(() => {
    if (singleTapTimeout.current) {
      clearTimeout(singleTapTimeout.current);
      singleTapTimeout.current = null;
    }
  }, []);

  const clearHoldSpeedTimer = useCallback(() => {
    if (holdSpeedTimeout.current) {
      clearTimeout(holdSpeedTimeout.current);
      holdSpeedTimeout.current = null;
    }
  }, []);

  const endHoldSpeed = useCallback(() => {
    clearHoldSpeedTimer();

    if (holdSpeedActiveRef.current && videoRef.current) {
      videoRef.current.playbackRate =
        holdSpeedRestoreRate.current || playbackSpeed;
    }

    holdSpeedActiveRef.current = false;
    setIsHoldSpeedActive(false);
  }, [clearHoldSpeedTimer, playbackSpeed]);

  const startHoldSpeed = useCallback(() => {
    if (!isTimedMedia || !isPlaying || !videoRef.current) return;

    holdSpeedRestoreRate.current =
      videoRef.current.playbackRate || playbackSpeed;
    videoRef.current.playbackRate = 2;
    holdSpeedActiveRef.current = true;
    setIsHoldSpeedActive(true);
    setShowControls(true);
  }, [isPlaying, isTimedMedia, playbackSpeed]);

  const minimizePlayer = useCallback(
    async (e?: React.MouseEvent<HTMLElement>) => {
      e?.stopPropagation();
      endHoldSpeed();
      await exitFullscreenIfActive();

      // Android Chromium/Kiwi needs one frame after exiting native fullscreen
      // before switching the player into the bottom mini layout. Otherwise the
      // mini player is rendered inside the fullscreen viewport and appears
      // stretched or misplaced.
      requestAnimationFrame(() => {
        setMinimized(true);
      });
    },
    [endHoldSpeed, exitFullscreenIfActive, setMinimized],
  );

  const clearSeekSettledTimer = useCallback(() => {
    if (seekSettledTimeout.current) {
      clearTimeout(seekSettledTimeout.current);
      seekSettledTimeout.current = null;
    }
  }, []);

  const clearSeekSpinnerTimer = useCallback(() => {
    if (seekSpinnerTimeout.current) {
      clearTimeout(seekSpinnerTimeout.current);
      seekSpinnerTimeout.current = null;
    }
  }, []);

  const clearPendingSeekCommit = useCallback(() => {
    if (pendingSeekCommitTimeout.current) {
      clearTimeout(pendingSeekCommitTimeout.current);
      pendingSeekCommitTimeout.current = null;
    }
    pendingSeekResumeOverrideRef.current = undefined;
  }, []);

  const beginSeekUi = useCallback(
    (targetTime: number) => {
      const seekRequestId = seekRequestIdRef.current + 1;
      seekRequestIdRef.current = seekRequestId;
      pendingSeekTargetRef.current = targetTime;
      setIsSeekingMedia(true);
      clearSeekSettledTimer();
      clearSeekSpinnerTimer();

      // YouTube-style seek UX: keep the current frame visible. Only show the
      // existing spinner if the target frame does not become ready quickly.
      seekSpinnerTimeout.current = setTimeout(() => {
        if (seekRequestIdRef.current === seekRequestId) {
          setIsLoading(true);
        }
        seekSpinnerTimeout.current = null;
      }, 180);

      // Safety fallback for browsers/streams that do not reliably emit seeked.
      seekSettledTimeout.current = setTimeout(() => {
        if (seekRequestIdRef.current !== seekRequestId) return;

        const mediaElement = mediaElementRef.current;
        const shouldResume = resumeAfterSeekRef.current;
        resumeAfterSeekRef.current = false;
        suppressProgrammaticSeekPauseRef.current = false;
        pendingSeekTargetRef.current = null;
        setIsSeekingMedia(false);
        clearSeekSpinnerTimer();

        if (mediaElement) {
          setCurrentTime(mediaElement.currentTime);
          mediaElement.playbackRate = playbackSpeed;
          if (mediaElement.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            setIsLoading(false);
          }

          if (shouldResume && !error) {
            mediaElement
              .play()
              .then(() => setIsPlaying(true))
              .catch(() => setIsPlaying(false));
          } else {
            setIsPlaying(!mediaElement.paused && !mediaElement.ended);
          }
        }

        seekSettledTimeout.current = null;
      }, 2500);
    },
    [clearSeekSettledTimer, clearSeekSpinnerTimer, error, playbackSpeed],
  );

  const commitSeekToTargetNow = useCallback(
  (targetTime: number, shouldResumeOverride?: boolean) => {
    const mediaElement = videoRef.current;
    if (!isTimedMedia || !mediaElement) return null;

    const mediaDuration = Number.isFinite(mediaElement.duration)
      ? mediaElement.duration
      : duration || file.duration || 0;

    const safeTime = Math.min(
      mediaDuration || Number.MAX_SAFE_INTEGER,
      Math.max(0, targetTime),
    );

    const wasPlaying = !mediaElement.paused && !mediaElement.ended;
    const shouldResume = shouldResumeOverride ?? wasPlaying;

    resumeAfterSeekRef.current = shouldResume;

    // Important fix:
    // If seekToTarget() already paused the media during debounced +10/-10 seek,
    // keep suppressing that artificial pause. Otherwise React UI may think user
    // manually paused the video and resume can behave weirdly.
    suppressProgrammaticSeekPauseRef.current =
      suppressProgrammaticSeekPauseRef.current || shouldResume || wasPlaying;

    beginSeekUi(safeTime);
    mediaElement.playbackRate = playbackSpeed;

    // Pause before setting currentTime. Android Chromium/Kiwi can paint
    // intermediate frames while seeking if the video keeps playing.
    if (wasPlaying) {
      mediaElement.pause();
    }

    try {
      mediaElement.currentTime = safeTime;
    } catch {
      // Some streams reject seek until metadata/range is ready.
    }

    setCurrentTime(safeTime);
    return safeTime;
  },
  [beginSeekUi, duration, file.duration, isTimedMedia, playbackSpeed],
);

  const seekToTarget = useCallback(
    (targetTime: number, shouldResumeOverride?: boolean) => {
      const mediaElement = videoRef.current;
      if (!isTimedMedia || !mediaElement) return null;

      const mediaDuration = Number.isFinite(mediaElement.duration)
        ? mediaElement.duration
        : duration || file.duration || 0;
      const safeTime = Math.min(
        mediaDuration || Number.MAX_SAFE_INTEGER,
        Math.max(0, targetTime),
      );

      const wasPlaying = !mediaElement.paused && !mediaElement.ended;
      const shouldResume =
        shouldResumeOverride ?? (wasPlaying || resumeAfterSeekRef.current);

      // Stop playback immediately, but delay the real currentTime write very
      // slightly. Rapid +10/-10 taps and keyboard repeat update only this
      // target, then commit once to the final position.
      if (wasPlaying) {
        suppressProgrammaticSeekPauseRef.current = true;
        mediaElement.pause();
      }

      mediaElement.playbackRate = playbackSpeed;
      pendingSeekTargetRef.current = safeTime;
      pendingSeekResumeOverrideRef.current = shouldResume;
      resumeAfterSeekRef.current = shouldResume;
      seekRequestIdRef.current += 1;
      setIsSeekingMedia(true);
      setCurrentTime(safeTime);
      clearSeekSettledTimer();
      clearSeekSpinnerTimer();
      clearPendingSeekCommit();

      pendingSeekResumeOverrideRef.current = shouldResume;
      pendingSeekCommitTimeout.current = setTimeout(() => {
        const queuedTarget = pendingSeekTargetRef.current ?? safeTime;
        const queuedResume =
          pendingSeekResumeOverrideRef.current ?? shouldResume;

        pendingSeekCommitTimeout.current = null;
        pendingSeekResumeOverrideRef.current = undefined;
        commitSeekToTargetNow(queuedTarget, queuedResume);
      }, SEEK_COMMIT_DEBOUNCE_MS);

      return safeTime;
    },
    [
      clearPendingSeekCommit,
      clearSeekSettledTimer,
      clearSeekSpinnerTimer,
      commitSeekToTargetNow,
      duration,
      file.duration,
      isTimedMedia,
      playbackSpeed,
    ],
  );

  const scheduleStartupRetry = useCallback(
    (reason: string) => {
      if (!isTimedMedia) return;

      clearDelayedError();
      clearStartupRetry();
      setError(null);
      setIsLoading(true);

      // If we have already shown metadata or a frame, this is no longer a startup issue.
      if (mediaLoadedMetadataRef.current || mediaHasPlayableDataRef.current) {
        return;
      }

      if (
        startupRetryCountRef.current >= 2 &&
        !usingFallbackStream &&
        file.fallback_stream_url
      ) {
        // Worker limits, regional cache failures, or an expired signed URL must
        // not make playback unavailable. Switch once to the authenticated origin.
        startupRetryCountRef.current = 0;
        mediaLoadToken.current += 1;
        mediaLoadedMetadataRef.current = false;
        mediaHasPlayableDataRef.current = false;
        setUsingFallbackStream(true);
        setStreamRetryNonce((value) => value + 1);
        return;
      }

      if (startupRetryCountRef.current >= MEDIA_STARTUP_MAX_RETRIES) {
        // Keep this separate from the real unsupported popup. It means stream startup
        // failed or Telegram/server stayed slow, not that the media format is bad.
        setError(
          `Stream did not start in time (${reason}). Try again, wait a moment, or download the file.`,
        );
        setIsLoading(false);
        return;
      }

      const retryToken = mediaLoadToken.current;
      startupRetryTimeout.current = setTimeout(() => {
        if (mediaLoadToken.current !== retryToken) return;

        startupRetryCountRef.current += 1;
        mediaLoadToken.current += 1;
        mediaLoadedMetadataRef.current = false;
        mediaHasPlayableDataRef.current = false;
        setStreamRetryNonce((value) => value + 1);
        setIsLoading(true);
      }, MEDIA_STARTUP_RETRY_DELAY_MS);
    },
    [
      clearDelayedError,
      clearStartupRetry,
      file.fallback_stream_url,
      isTimedMedia,
      usingFallbackStream,
    ],
  );

  const handleTimeUpdate = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    const mediaElement = mediaElementRef.current;
    if (mediaElement) {
      const pendingSeekTarget = pendingSeekTargetRef.current;
      if (pendingSeekTarget !== null && mediaElement.seeking) {
        setCurrentTime(pendingSeekTarget);
        return;
      }

      if (mediaElement.currentTime > 1) {
        clearedProgressFileId.current = null;
      }
      setCurrentTime(mediaElement.currentTime);
    }
  };

  const handleLoadedMetadata = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    clearDelayedError();
    clearStartupRetry();
    mediaLoadedMetadataRef.current = true;
    startupRetryCountRef.current = 0;

    const mediaElement = mediaElementRef.current;
    if (mediaElement) {
      setDuration(mediaElement.duration);
      mediaElement.playbackRate = playbackSpeed;
      setIsLoading(false);
      restoreSavedProgress();
    }
  };

  const handleMediaReady = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    clearDelayedError();
    clearStartupRetry();
    mediaHasPlayableDataRef.current = true;
    startupRetryCountRef.current = 0;

    const mediaElement = mediaElementRef.current;
    if (!mediaElement?.seeking && pendingSeekTargetRef.current === null) {
      setIsLoading(false);
    }
  };

  const handleWaiting = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    clearDelayedError();
    setIsLoading(true);
  };

  const handlePlaying = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    clearDelayedError();
    clearStartupRetry();
    mediaHasPlayableDataRef.current = true;
    startupRetryCountRef.current = 0;

    const mediaElement = mediaElementRef.current;
    if (!mediaElement?.seeking && pendingSeekTargetRef.current === null) {
      setIsLoading(false);
    }
  };

  const handleSeeking = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    setIsSeekingMedia(true);

    // Browser/native seeks that were not started by our 10s/progress controls
    // should still show the normal spinner immediately. Our own seeks use a
    // short delay so fast seeks feel like YouTube instead of flickering.
    if (pendingSeekTargetRef.current === null) {
      setIsLoading(true);
    }
  };

  const handleSeeked = (event?: React.SyntheticEvent<HTMLMediaElement>) => {
    if (!isCurrentMediaEvent(event)) return;

    const mediaElement = mediaElementRef.current;
    const pendingSeekTarget = pendingSeekTargetRef.current;

    // Very fast repeated +10/-10 taps can emit an older seeked event after a
    // newer target has already been requested. Ignore it until the media is at
    // the latest target, so the spinner/progress do not settle early.
    if (
      mediaElement &&
      pendingSeekTarget !== null &&
      Math.abs(mediaElement.currentTime - pendingSeekTarget) > 1.25
    ) {
      return;
    }

    clearSeekSettledTimer();
    clearSeekSpinnerTimer();

    const shouldResume = resumeAfterSeekRef.current;
    resumeAfterSeekRef.current = false;
    suppressProgrammaticSeekPauseRef.current = false;

    if (mediaElement) {
      setCurrentTime(mediaElement.currentTime);
      mediaElement.playbackRate = playbackSpeed;

      if (mediaElement.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
        setIsLoading(false);
      }

      if (shouldResume && !error) {
        mediaElement
          .play()
          .then(() => setIsPlaying(true))
          .catch(() => setIsPlaying(false));
      } else {
        setIsPlaying(!mediaElement.paused && !mediaElement.ended);
      }
    }

    pendingSeekTargetRef.current = null;
    pendingSeekResumeOverrideRef.current = undefined;
    if (pendingSeekCommitTimeout.current) {
      clearTimeout(pendingSeekCommitTimeout.current);
      pendingSeekCommitTimeout.current = null;
    }
    seekRequestIdRef.current += 1;
    setIsSeekingMedia(false);
  };

  const handlePlayStateChange = (
    event: React.SyntheticEvent<HTMLMediaElement>,
    playing: boolean,
  ) => {
    if (!isCurrentMediaEvent(event)) return;

    // Ignore the artificial pause we do before programmatic seek.
    // This prevents play/pause UI flicker during +10/-10 seek.
    if (!playing && suppressProgrammaticSeekPauseRef.current) return;

    setIsPlaying(playing);
  };

  const handleError = (
    event?: React.SyntheticEvent<HTMLMediaElement | HTMLImageElement>,
  ) => {
    const target = event?.currentTarget;
    const currentSrc =
      target && "currentSrc" in target
        ? target.currentSrc || target.getAttribute("src") || ""
        : authorizedStreamUrl;

    if (target && "tagName" in target && target instanceof HTMLMediaElement) {
      if (!isCurrentMediaEvent(event as React.SyntheticEvent<HTMLMediaElement>)) {
        return;
      }
    }

    if (currentSrc && currentSrc !== authorizedStreamUrl) {
      return;
    }

    const loadToken = mediaLoadToken.current;

    if (isImage) {
      showErrorAfterDelay(
        PLAYER_ERROR_MESSAGES.unsupportedImage,
        authorizedStreamUrl,
        loadToken,
      );
      return;
    }

    const mediaElement = videoRef.current;
    const mediaError = mediaElement?.error;
    if (!mediaElement || !mediaError) return;

    // Ignore aborts/cancellations from fast next/previous/card switching.
    if (mediaError.code === MEDIA_ERR_ABORTED) {
      clearDelayedError();
      clearStartupRetry();
      setIsLoading(true);
      return;
    }

    // Before metadata/first frame, browser errors are often slow stream startup,
    // cancelled range requests, or Telegram taking too long. Never show the
    // unsupported-format popup from this phase; retry the stream instead.
    const isStartupPhase =
      !mediaLoadedMetadataRef.current &&
      !mediaHasPlayableDataRef.current &&
      mediaElement.readyState < HTMLMediaElement.HAVE_METADATA;

    if (isStartupPhase) {
      scheduleStartupRetry(`media error ${mediaError.code}`);
      return;
    }

    // Network problems after metadata are buffering/retry problems, not unsupported format.
    if (mediaError.code === MEDIA_ERR_NETWORK) {
      scheduleStartupRetry("network error");
      return;
    }

    const code = mediaError.code;
    const message =
      code === MEDIA_ERR_DECODE ||
      code === MEDIA_ERR_SRC_NOT_SUPPORTED
        ? PLAYER_ERROR_MESSAGES.unsupportedMedia
        : PLAYER_ERROR_MESSAGES.genericMedia;

    // Wait for the configured delay and re-check the same file/src before showing it.
    showErrorAfterDelay(message, authorizedStreamUrl, loadToken);
  };

  const showSeekFeedback = useCallback((direction: "back" | "forward") => {
    if (seekFeedbackTimeout.current) {
      clearTimeout(seekFeedbackTimeout.current);
    }

    setSeekFeedback({ direction, key: Date.now() });
    seekFeedbackTimeout.current = setTimeout(() => {
      setSeekFeedback(null);
      seekFeedbackTimeout.current = null;
    }, 650);
  }, []);

  const handleSkip = useCallback(
    (seconds: number, showFeedback = false) => {
      const mediaElement = videoRef.current;
      if (!isTimedMedia || !mediaElement) return;

      endHoldSpeed();

      const mediaDuration = Number.isFinite(mediaElement.duration)
        ? mediaElement.duration
        : duration || file.duration || 0;
      const baseTime = pendingSeekTargetRef.current ?? mediaElement.currentTime;
      const nextTime = Math.min(
        mediaDuration || Number.MAX_SAFE_INTEGER,
        Math.max(0, baseTime + seconds),
      );

      userChangedPositionRef.current = true;
      restoredProgressFileId.current = file.id;
      seekToTarget(nextTime);

      if (showFeedback) {
        showSeekFeedback(seconds < 0 ? "back" : "forward");
      }
    },
    [
      duration,
      endHoldSpeed,
      file.duration,
      file.id,
      isTimedMedia,
      seekToTarget,
      showSeekFeedback,
    ],
  );

  const playMediaAtIndex = useCallback(
    (index: number, shouldSaveProgress = true) => {
      if (!previewQueue.length) return false;

      const now = Date.now();
      if (now - lastMediaSwitchAt.current < MEDIA_SWITCH_THROTTLE_MS) {
        return false;
      }
      lastMediaSwitchAt.current = now;

      const safeIndex =
        ((index % previewQueue.length) + previewQueue.length) %
        previewQueue.length;
      const nextFile = previewQueue[safeIndex];

      if (!nextFile || nextFile.id === file.id) return false;

      if (shouldSaveProgress) {
        saveProgress();
      }

      mediaLoadToken.current += 1;
      clearDelayedError();
      clearStartupRetry();
      abortCurrentMediaLoad();
      setCurrentTime(0);
      setDuration(0);
      setIsPlaying(false);
      setIsLoading(true);
      setError(null);
      setPublicUrl(null);
      setPreviewFile(nextFile);
      return true;
    },
    [abortCurrentMediaLoad, clearDelayedError, clearStartupRetry, file.id, previewQueue, saveProgress, setPreviewFile],
  );

  const playNextMedia = useCallback(() => {
    if (!hasMultipleMedia) return false;

    const nextIndex = (currentMediaIndex + 1) % previewQueue.length;
    return playMediaAtIndex(nextIndex);
  }, [
    currentMediaIndex,
    hasMultipleMedia,
    playMediaAtIndex,
    previewQueue.length,
  ]);

  const playPreviousMedia = useCallback(() => {
    if (!hasMultipleMedia) return false;

    const previousIndex =
      currentMediaIndex === 0 ? previewQueue.length - 1 : currentMediaIndex - 1;
    return playMediaAtIndex(previousIndex);
  }, [
    currentMediaIndex,
    hasMultipleMedia,
    playMediaAtIndex,
    previewQueue.length,
  ]);

  useEffect(() => {
    if (isTrashFile || !hasMultipleMedia || currentMediaIndex === -1) return;

    const ids = new Set<number>();
    const previousIndex =
      currentMediaIndex === 0 ? previewQueue.length - 1 : currentMediaIndex - 1;
    const nextIndex = (currentMediaIndex + 1) % previewQueue.length;

    const previousFile = previewQueue[previousIndex];
    const nextFile = previewQueue[nextIndex];

    if (previousFile && previousFile.id !== file.id) ids.add(previousFile.id);
    if (nextFile && nextFile.id !== file.id) ids.add(nextFile.id);

    const fileIds = Array.from(ids).slice(0, 2);
    if (!fileIds.length) return;

    if (neighborPrefetchTimeout.current) {
      clearTimeout(neighborPrefetchTimeout.current);
    }

    neighborPrefetchTimeout.current = setTimeout(() => {
      api.post("/stream/prefetch", { file_ids: fileIds }).catch(() => {
        // Prefetch is optional. Ignore unsupported older backends or Redis-off setups.
      });
      neighborPrefetchTimeout.current = null;
    }, 250);

    return () => {
      if (neighborPrefetchTimeout.current) {
        clearTimeout(neighborPrefetchTimeout.current);
        neighborPrefetchTimeout.current = null;
      }
    };
  }, [currentMediaIndex, file.id, hasMultipleMedia, isTrashFile, previewQueue]);


  const playCurrentFromStart = useCallback(() => {
    if (!videoRef.current) return;

    userChangedPositionRef.current = true;
    restoredProgressFileId.current = file.id;
    seekToTarget(0, true);
    setCurrentTime(0);
  }, [file.id, seekToTarget]);

  const stopCurrentAtStart = useCallback(() => {
    if (videoRef.current) {
      userChangedPositionRef.current = true;
      restoredProgressFileId.current = file.id;
      seekToTarget(0, false);
    }

    setCurrentTime(0);
    setIsPlaying(false);
    setIsLoading(false);
  }, [file.id, seekToTarget]);

  const getRandomMediaIndex = useCallback(() => {
    if (previewQueue.length <= 1 || currentMediaIndex === -1) {
      return currentMediaIndex;
    }

    let nextIndex = currentMediaIndex;
    for (
      let attempt = 0;
      attempt < 10 && nextIndex === currentMediaIndex;
      attempt += 1
    ) {
      nextIndex = Math.floor(Math.random() * previewQueue.length);
    }

    return nextIndex === currentMediaIndex
      ? (currentMediaIndex + 1) % previewQueue.length
      : nextIndex;
  }, [currentMediaIndex, previewQueue.length]);

  const handleEnded = useCallback(() => {
    if (!isTimedMedia) return;

    clearDelayedError();
    setIsPlaying(false);
    setIsLoading(false);

    // Reset progress so a finished video/audio does not reopen stuck at the end.
    saveProgress(0);

    // Deleted media is a single read-only preview. Do not advance, shuffle,
    // repeat through a queue, or trigger any neighbour behavior.
    if (isTrashFile) {
      stopCurrentAtStart();
      return;
    }

    if (playbackMode === "repeat_one") {
      playCurrentFromStart();
      return;
    }

    if (playbackMode === "autoplay_off") {
      stopCurrentAtStart();
      return;
    }

    if (playbackMode === "stop_after_this") {
      stopCurrentAtStart();
      applyPlaybackMode("normal");
      return;
    }

    if (hasMultipleMedia) {
      const nextIndex =
        playbackMode === "shuffle"
          ? getRandomMediaIndex()
          : (currentMediaIndex + 1) % previewQueue.length;

      if (nextIndex !== -1 && playMediaAtIndex(nextIndex, false)) return;
    }

    // Normal mode loops a single-file queue too.
    playCurrentFromStart();
  }, [
    applyPlaybackMode,
    clearDelayedError,
    currentMediaIndex,
    getRandomMediaIndex,
    hasMultipleMedia,
    isTimedMedia,
    isTrashFile,
    playCurrentFromStart,
    playMediaAtIndex,
    playbackMode,
    previewQueue.length,
    saveProgress,
    stopCurrentAtStart,
  ]);

  const getBoundedSeekTime = useCallback(
    (value: string | number) => {
      const requestedTime =
        typeof value === "number" ? value : parseFloat(value);
      const mediaDuration = duration || file.duration || 0;

      if (!Number.isFinite(requestedTime)) {
        return currentTime;
      }

      return Math.min(
        mediaDuration || Number.MAX_SAFE_INTEGER,
        Math.max(0, requestedTime),
      );
    },
    [currentTime, duration, file.duration],
  );

  const commitSeek = useCallback(
    (time: number) => {
      if (!isTimedMedia || !videoRef.current) return;

      endHoldSpeed();
      userChangedPositionRef.current = true;
      restoredProgressFileId.current = file.id;
      setSeekPreviewTime(null);

      const actualTime = seekToTarget(time);

      if ((actualTime ?? time) <= 1) {
        saveProgress(0);
      }
    },
    [endHoldSpeed, file.id, isTimedMedia, saveProgress, seekToTarget],
  );

  const handleSeekPointerDown = () => {
    if (!isTimedMedia || !videoRef.current) return;

    isScrubbingSeekRef.current = true;
    endHoldSpeed();
    setSeekPreviewTime(currentTime);
  };

  const handleSeekPreview = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!isTimedMedia || !videoRef.current) return;

    const time = getBoundedSeekTime(e.target.value);
    setSeekPreviewTime(time);

    // Keyboard changes do not go through the pointer drag flow, so commit them
    // immediately. Pointer/touch drags only seek once, when the user releases.
    if (!isScrubbingSeekRef.current) {
      commitSeek(time);
    }
  };

  const handleSeekCommit = (e: React.SyntheticEvent<HTMLInputElement>) => {
    if (!isScrubbingSeekRef.current) return;

    isScrubbingSeekRef.current = false;
    commitSeek(getBoundedSeekTime(e.currentTarget.value));
  };

  const handleSeekCancel = () => {
    isScrubbingSeekRef.current = false;
    setSeekPreviewTime(null);
  };

  const handleVolumeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (videoRef.current) {
      const vol = parseFloat(e.target.value);
      videoRef.current.volume = vol;
      setVolume(vol);
      setIsMuted(vol === 0);
    }
  };

  const closePlayer = useCallback(() => {
    mediaLoadToken.current += 1;
    clearDelayedError();
    clearStartupRetry();
    clearSeekSettledTimer();
    clearSeekSpinnerTimer();
    pendingSeekTargetRef.current = null;
    clearPendingSeekCommit();
    seekRequestIdRef.current += 1;
    resumeAfterSeekRef.current = false;
    suppressProgrammaticSeekPauseRef.current = false;
    setIsSeekingMedia(false);

    if (document.fullscreenElement === containerRef.current) {
      document.exitFullscreen().catch(() => undefined);
      setIsFullscreen(false);
    }

    abortCurrentMediaLoad();
    onClose();
  }, [
    abortCurrentMediaLoad,
    clearDelayedError,
    clearSeekSettledTimer,
    clearSeekSpinnerTimer,
    clearStartupRetry,
    clearPendingSeekCommit,
    onClose,
  ]);

  // Keyboard controls
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (error) return;

      switch (e.key) {
        case " ":
        case "k":
          if (!isTimedMedia) return;
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowLeft":
        case "j":
          if (!isTimedMedia) return;
          e.preventDefault();
          handleSkip(-10);
          break;
        case "ArrowRight":
        case "l":
          if (!isTimedMedia) return;
          e.preventDefault();
          handleSkip(10);
          break;
        case "m":
          if (isTimedMedia) toggleMute();
          break;
        case "f":
          toggleFullscreen();
          break;
        case "Escape":
          if (isFullscreen) {
            document.exitFullscreen();
          } else if (!isMinimized) {
            closePlayer();
          }
          break;
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isFullscreen, closePlayer, error, isPlaying, isMinimized, isTimedMedia, handleSkip, toggleMute, togglePlay]);

  const clearHideControlsTimer = useCallback(() => {
    if (hideControlsTimeout.current) {
      clearTimeout(hideControlsTimeout.current);
      hideControlsTimeout.current = null;
    }
  }, []);

  const startHideControlsTimer = useCallback(() => {
    clearHideControlsTimer();

    if (isMinimized || error) return;

    hideControlsTimeout.current = setTimeout(() => {
      setShowControls(false);
    }, 3000);
  }, [clearHideControlsTimer, error, isMinimized]);

  const revealControlsTemporarily = useCallback(() => {
    if (isMinimized || error) return;

    setShowControls(true);
    startHideControlsTimer();
  }, [error, isMinimized, startHideControlsTimer]);

  const handlePointerActivity = useCallback(() => {
    revealControlsTemporarily();
  }, [revealControlsTemporarily]);

  const toggleSurfaceControls = useCallback(() => {
    if (showControls) {
      clearHideControlsTimer();
      setShowControls(false);
    } else {
      setShowControls(true);
      startHideControlsTimer();
    }
  }, [clearHideControlsTimer, showControls, startHideControlsTimer]);

  const isPlayerControlTarget = useCallback(
    (target: EventTarget | null) =>
      target instanceof Element &&
      !!target.closest(
        '[data-player-control="true"], button, a, input, textarea, select',
      ),
    [],
  );

  const handleGesturePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (
        isMinimized ||
        error ||
        !isTimedMedia ||
        isPlayerControlTarget(e.target)
      ) {
        return;
      }

      gesturePointerId.current = e.pointerId;
      gesturePointerStart.current = { x: e.clientX, y: e.clientY };
      suppressNextSurfaceTap.current = false;
      clearHoldSpeedTimer();

      holdSpeedTimeout.current = setTimeout(() => {
        startHoldSpeed();
      }, 360);
    },
    [
      clearHoldSpeedTimer,
      error,
      isMinimized,
      isPlayerControlTarget,
      isTimedMedia,
      startHoldSpeed,
    ],
  );

  const handleGesturePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (gesturePointerId.current !== e.pointerId) return;

      const dx = e.clientX - gesturePointerStart.current.x;
      const dy = e.clientY - gesturePointerStart.current.y;
      const moved = Math.hypot(dx, dy);

      if (moved > 18 && !holdSpeedActiveRef.current) {
        clearHoldSpeedTimer();
      }
    },
    [clearHoldSpeedTimer],
  );

  const finishGesturePointer = useCallback(
    (e?: React.PointerEvent<HTMLDivElement>) => {
      if (e && gesturePointerId.current !== e.pointerId) return;

      const wasHolding = holdSpeedActiveRef.current;
      clearHoldSpeedTimer();
      gesturePointerId.current = null;

      if (wasHolding) {
        suppressNextSurfaceTap.current = true;
        endHoldSpeed();
      }
    },
    [clearHoldSpeedTimer, endHoldSpeed],
  );

  const handleSurfaceTap = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (isMinimized || error) return;

      const target = e.target as HTMLElement | null;
      if (isPlayerControlTarget(target)) {
        revealControlsTemporarily();
        return;
      }

      if (suppressNextSurfaceTap.current) {
        suppressNextSurfaceTap.current = false;
        return;
      }

      if (!isTimedMedia) {
        toggleSurfaceControls();
        return;
      }

      const rect = e.currentTarget.getBoundingClientRect();
      const side = e.clientX - rect.left < rect.width / 2 ? "left" : "right";
      const now = Date.now();
      const previousTap = lastTapRef.current;

      if (
        previousTap &&
        previousTap.side === side &&
        now - previousTap.time <= 320
      ) {
        clearSingleTapTimer();
        lastTapRef.current = null;
        handleSkip(side === "left" ? -10 : 10, true);
        revealControlsTemporarily();
        return;
      }

      lastTapRef.current = { time: now, side };
      clearSingleTapTimer();
      singleTapTimeout.current = setTimeout(() => {
        lastTapRef.current = null;
        toggleSurfaceControls();
        singleTapTimeout.current = null;
      }, 320);
    },
    [
      clearSingleTapTimer,
      error,
      handleSkip,
      isMinimized,
      isPlayerControlTarget,
      isTimedMedia,
      revealControlsTemporarily,
      toggleSurfaceControls,
    ],
  );

  const handleSurfaceContextMenu = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (!isMinimized && isTimedMedia && !isPlayerControlTarget(e.target)) {
        e.preventDefault();
      }
    },
    [isMinimized, isPlayerControlTarget, isTimedMedia],
  );

  // MX Player-style auto-hide: show controls, then hide after 3 seconds untouched.
  useEffect(() => {
    if (!isMinimized && !error) {
      setShowControls(true);
      startHideControlsTimer();
    }

    return clearHideControlsTimer;
  }, [
    clearHideControlsTimer,
    error,
    file.id,
    isMinimized,
    startHideControlsTimer,
  ]);

  // Auto-play effect for audio/video. Images just wait for onLoad.
  useEffect(() => {
    if (isTimedMedia && videoRef.current && !error) {
      videoRef.current
        .play()
        .then(() => setIsPlaying(true))
        .catch(() => {
          // Autoplay may be blocked; that is not a playback-format error.
          setIsPlaying(false);
        });
    }
  }, [isTimedMedia, error, file.id, streamRetryNonce]);

  const displayedTime = seekPreviewTime ?? currentTime;
  const displayedDuration = duration || file.duration || 0;
  const progressPercent =
    isTimedMedia && displayedDuration > 0
      ? (displayedTime / displayedDuration) * 100
      : 0;

  const MediaElement = isVideo ? (
    <video
      key={`${file.id}:${streamRetryNonce}`}
      ref={setMediaElementRef}
      src={authorizedStreamUrl}
      crossOrigin={mediaCrossOrigin}
      className={`w-full h-full ${isMinimized ? "object-cover" : "max-w-full max-h-full object-contain"}`}
      onTimeUpdate={handleTimeUpdate}
      onLoadedMetadata={handleLoadedMetadata}
      onLoadedData={handleMediaReady}
      onCanPlay={handleMediaReady}
      onSeeking={handleSeeking}
      onSeeked={handleSeeked}
      onWaiting={handleWaiting}
      onPlaying={handlePlaying}
      onPlay={(event) => handlePlayStateChange(event, true)}
      onPause={(event) => handlePlayStateChange(event, false)}
      onEnded={handleEnded}
      onError={handleError}
      controls={false}
      preload="auto"
      playsInline
    />
  ) : isAudio ? (
    <audio
      key={`${file.id}:${streamRetryNonce}`}
      ref={setMediaElementRef}
      src={authorizedStreamUrl}
      crossOrigin={mediaCrossOrigin}
      onTimeUpdate={handleTimeUpdate}
      onLoadedMetadata={handleLoadedMetadata}
      onLoadedData={handleMediaReady}
      onCanPlay={handleMediaReady}
      onSeeking={handleSeeking}
      onSeeked={handleSeeked}
      onWaiting={handleWaiting}
      onPlaying={handlePlaying}
      onPlay={(event) => handlePlayStateChange(event, true)}
      onPause={(event) => handlePlayStateChange(event, false)}
      onEnded={handleEnded}
      onError={handleError}
      preload="auto"
    />
  ) : (
    <img
      key={file.id}
      src={authorizedStreamUrl}
      alt={file.file_name}
      crossOrigin={mediaCrossOrigin}
      className="max-w-full max-h-full w-full h-full object-contain select-none"
      onLoad={() => {
        clearDelayedError();
        setIsLoading(false);
      }}
      onError={handleError}
      draggable={false}
    />
  );

  // Unified Render
  return (
    <div
      ref={containerRef}
      className={`fixed transition-all duration-300 ease-in-out z-[100] ${
        isMinimized
          ? "bottom-0 left-0 right-0 h-20 bg-dark-900 border-t border-white/10 shadow-2xl"
          : "inset-0 bg-black flex items-center justify-center font-sans"
      }`}
      onMouseMove={!isMinimized ? handlePointerActivity : undefined}
      onPointerDown={!isMinimized ? handleGesturePointerDown : undefined}
      onPointerMove={!isMinimized ? handleGesturePointerMove : undefined}
      onPointerUp={!isMinimized ? finishGesturePointer : undefined}
      onPointerCancel={!isMinimized ? finishGesturePointer : undefined}
      onPointerLeave={!isMinimized ? finishGesturePointer : undefined}
      onClick={!isMinimized ? handleSurfaceTap : undefined}
      onContextMenu={!isMinimized ? handleSurfaceContextMenu : undefined}
    >
      {/* Media Element - Always present */}
      <div
        className={
          isMinimized && isVideo
            ? "absolute left-3 top-3 w-14 h-14 sm:w-16 sm:h-14 rounded-lg bg-dark-800 flex items-center justify-center overflow-hidden border border-white/10 z-[102] cursor-pointer shadow-lg"
            : `w-full h-full ${isMinimized ? "hidden" : "flex items-center justify-center"}`
        }
        onClick={
          isMinimized && isVideo
            ? (e) => {
                e.stopPropagation();
                setMinimized(false);
              }
            : undefined
        }
      >
        {error ? (
          <div className="text-center p-8 max-w-md glass-panel z-10 animate-scale-in">
            <div className="w-16 h-16 rounded-2xl bg-yellow-500/20 flex items-center justify-center mx-auto mb-5 border border-yellow-500/30">
              <AlertTriangle className="w-8 h-8 text-yellow-400" />
            </div>
            <h3 className="text-xl font-bold text-white mb-2">
              {isImage ? "Preview Not Supported" : "Playback Not Supported"}
            </h3>
            <p className="text-dark-300 mb-6">{error}</p>

            {!isTrashFile && (
              <div className="flex flex-col gap-3">
                {!isImage && (
                  <a
                    href={vlcUrl}
                    className="btn-primary flex items-center justify-center gap-2"
                  >
                    <ExternalLink className="w-4 h-4" />
                    Open in VLC
                  </a>
                )}
                <div className="flex gap-3">
                  <Button
                    onClick={(e) => {
                      e.stopPropagation();
                      navigator.clipboard.writeText(externalUrl);
                    }}
                    className="flex-1 btn-secondary flex items-center justify-center gap-2"
                  >
                    <Copy className="w-4 h-4" />
                    Copy URL
                  </Button>
                  <a
                    href={
                      externalUrl +
                      (externalUrl.includes("?") ? "&" : "?") +
                      "download=1"
                    }
                    download={file.file_name}
                    className="flex-1 btn-secondary flex items-center justify-center gap-2"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Download className="w-4 h-4" />
                    Download
                  </a>
                </div>
              </div>
            )}
            <button
              onClick={closePlayer}
              className="mt-6 text-dark-400 hover:text-white text-sm transition-colors"
            >
              Close
            </button>
          </div>
        ) : (
          <>
            {/* Audio artwork / Thumbnail for audio files in Fullscreen */}
            {isAudio && !isMinimized && (
              <div className="text-center z-10 glass-panel p-8 sm:p-10 animate-scale-in relative flex flex-col items-center justify-center min-w-[280px]">
                <AudioArtworkPreview
                  thumbnailUrl={authorizedThumbnailUrl}
                  fileName={file.file_name}
                  isPlaying={isPlaying}
                />
                <p className="text-2xl font-bold text-white mb-2 drop-shadow-md max-w-[80vw] truncate">
                  {file.file_name}
                </p>
                <p className="text-primary-400 font-medium">
                  {formatDuration(displayedTime)} / {formatDuration(displayedDuration)}
                </p>
              </div>
            )}

            {MediaElement}

            {seekFeedback && !isMinimized && (
              <div
                key={seekFeedback.key}
                className={`pointer-events-none absolute inset-y-0 ${
                  seekFeedback.direction === "back" ? "left-0" : "right-0"
                } w-1/2 flex items-center justify-center z-20`}
              >
                <div className="px-5 py-3 rounded-full bg-black/55 border border-white/10 text-white text-lg font-semibold backdrop-blur-md shadow-2xl animate-scale-in">
                  {seekFeedback.direction === "back" ? "↺ 10s" : "10s ↻"}
                </div>
              </div>
            )}

            {isHoldSpeedActive && !isMinimized && (
              <div className="pointer-events-none absolute top-20 left-1/2 -translate-x-1/2 z-30 px-4 py-2 rounded-full bg-primary-600/80 border border-white/15 text-white text-sm font-bold backdrop-blur-md shadow-lg shadow-primary-500/20 animate-scale-in">
                Hold 2x
              </div>
            )}

            {/* Loading Spinner - YouTube-style: no dark/black cover, video frame stays visible. */}
            {isLoading && !error && !isMinimized && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-20">
                <div className="animate-spin rounded-full h-16 w-16 border-b-2 border-primary-500"></div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Minimized Controls */}
      {isMinimized && (
        <div className="max-w-7xl mx-auto flex items-center justify-between gap-2 sm:gap-4 p-3 h-full relative">
          <div
            className="flex items-center gap-3 overflow-hidden flex-1 cursor-pointer min-w-0"
            onClick={() => setMinimized(false)}
          >
            {/* Thumbnail/Icon */}
            <div className="w-12 h-12 rounded-lg bg-dark-800 flex items-center justify-center flex-shrink-0 overflow-hidden border border-white/5 relative">
              {isVideo ? (
                <span className="text-2xl opacity-0">🎬</span>
              ) : isAudio ? (
                <MiniAudioArtwork
                  thumbnailUrl={minimizedThumbUrl}
                  fileName={file.file_name}
                  isPlaying={isPlaying}
                />
              ) : minimizedThumbUrl ? (
                <img
                  src={minimizedThumbUrl}
                  alt="Thumb"
                  crossOrigin={mediaCrossOrigin}
                  className="w-full h-full object-cover"
                />
              ) : isImage ? (
                <ImageIcon className="w-6 h-6 text-emerald-400" />
              ) : (
                <span className="text-2xl">🎵</span>
              )}
            </div>
            <div className="truncate flex-1 min-w-0">
              <h4 className="text-sm font-bold text-white truncate leading-tight">
                {file.file_name}
              </h4>
              <p className="text-xs text-dark-400 font-mono">
                {isTimedMedia
                  ? `${formatDuration(currentTime)} / ${formatDuration(duration)}`
                  : "Image preview"}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-1 sm:gap-3 shrink-0">
            {!isTrashFile && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  playPreviousMedia();
                }}
                className="p-2 text-dark-300 hover:text-white disabled:opacity-40"
                title="Previous file"
                disabled={!hasMultipleMedia}
              >
                <SkipBack className="w-5 h-5" />
              </button>
            )}
            {isTimedMedia && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  togglePlay();
                }}
                className="p-2 bg-primary-600 rounded-full text-white hover:bg-primary-500 shadow-lg shadow-primary-500/20"
              >
                {isPlaying ? (
                  <Pause className="w-5 h-5" />
                ) : (
                  <Play className="w-5 h-5" />
                )}
              </button>
            )}
            {!isTrashFile && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  playNextMedia();
                }}
                className="p-2 text-dark-300 hover:text-white disabled:opacity-40"
                title="Next file"
                disabled={!hasMultipleMedia}
              >
                <SkipForward className="w-5 h-5" />
              </button>
            )}
            {isTimedMedia && !isTrashFile && (
              <PlaybackModeButton
                mode={currentPlaybackMode}
                onClick={cyclePlaybackMode}
                compact
              />
            )}
          </div>

          <div className="flex items-center gap-1 sm:gap-2 border-l border-white/10 pl-2 sm:pl-4 shrink-0">
            <button
              onClick={() => setMinimized(false)}
              className="p-2 text-dark-400 hover:text-white"
              title="Maximize"
            >
              <ChevronUp className="w-5 h-5" />
            </button>
            <button
              onClick={closePlayer}
              className="p-2 text-dark-400 hover:text-red-400"
              title="Close"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Progress bar line at top */}
          {isTimedMedia && (
            <div className="absolute top-0 left-0 right-0 h-0.5 bg-dark-800">
              <div
                className="h-full bg-primary-500"
                style={{ width: `${progressPercent}%` }}
              ></div>
            </div>
          )}
        </div>
      )}

      {/* Fullscreen Controls overlay */}
      {!error && !isMinimized && (
        <div
          className={`absolute inset-0 transition-opacity duration-300 ${showControls ? "opacity-100" : "opacity-0 cursor-none"}`}
          style={{
            pointerEvents: showControls ? "auto" : "none",
          }}
        >
          {/* Top bar */}
          <div
            data-player-control="true"
            className="absolute top-0 left-0 right-0 p-3 sm:p-4 bg-gradient-to-b from-black/80 to-transparent flex items-start justify-between gap-3 z-30"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="min-w-0 flex-1 pr-1">
              <h3
                className="text-base sm:text-lg font-medium truncate text-white"
                title={file.file_name}
              >
                {file.file_name}
              </h3>
              {isTimedMedia &&
                (extendedFile?.last_pos ?? file.last_pos ?? 0) > 0 &&
                currentTime < 5 && (
                  <p className="text-xs text-primary-400 truncate">
                    Resumed from{" "}
                    {formatDuration(extendedFile?.last_pos ?? file.last_pos ?? 0)}
                  </p>
                )}
            </div>
            <div className="flex items-center gap-1 sm:gap-2 shrink-0">
              <button
                onClick={minimizePlayer}
                className="p-2 text-white hover:bg-white/20 rounded-full transition-colors"
                title="Minimize"
              >
                <ChevronDown className="w-5 h-5 sm:w-6 sm:h-6" />
              </button>
              <button
                onClick={closePlayer}
                className="p-2 text-white hover:bg-white/20 rounded-full transition-colors"
                title="Close"
              >
                <X className="w-5 h-5 sm:w-6 sm:h-6" />
              </button>
            </div>
          </div>

          {/* Center large controls: 10 sec buttons are not previous/next */}
          {isTimedMedia && (
            <div
              data-player-control="true"
              className="absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 flex items-center gap-4 sm:gap-8 z-30"
              onClick={(e) => e.stopPropagation()}
            >
              <TenSecondButton
                direction="back"
                onClick={(e) => {
                  e.stopPropagation();
                  handleSkip(-10);
                }}
              />

              <button
                onClick={(e) => {
                  e.stopPropagation();
                  togglePlay();
                }}
                className="w-16 h-16 sm:w-20 sm:h-20 bg-white/20 backdrop-blur rounded-full flex items-center justify-center hover:bg-white/30 transition-all scale-100 hover:scale-105"
                title={isPlaying ? "Pause" : "Play"}
              >
                {isPlaying ? (
                  <Pause className="w-8 h-8 sm:w-10 sm:h-10 text-white" />
                ) : (
                  <Play className="w-8 h-8 sm:w-10 sm:h-10 ml-1 text-white" />
                )}
              </button>

              <TenSecondButton
                direction="forward"
                onClick={(e) => {
                  e.stopPropagation();
                  handleSkip(10);
                }}
              />
            </div>
          )}

          {/* Bottom controls */}
          <div
            data-player-control="true"
            className="absolute bottom-0 left-0 right-0 p-3 sm:p-6 bg-gradient-to-t from-black/95 via-black/70 to-transparent z-30"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Progress bar */}
            {isTimedMedia && (
              <div className="flex items-center gap-2 sm:gap-4 mb-4 group/progress">
                <span className="text-xs sm:text-sm font-medium text-white/90 min-w-[42px] sm:min-w-[50px] font-mono">
                  {formatDuration(Math.floor(displayedTime))}
                </span>
                <div className="relative flex-1 h-1 bg-white/20 rounded-full cursor-pointer group-hover/progress:h-2 transition-all">
                  <div
                    className="absolute inset-y-0 left-0 bg-gradient-to-r from-primary-500 to-primary-400 rounded-full"
                    style={{ width: `${progressPercent}%` }}
                  >
                    <div className="absolute right-0 top-1/2 transform -translate-y-1/2 w-4 h-4 bg-white rounded-full opacity-0 group-hover/progress:opacity-100 transition-all shadow-lg shadow-primary-500/50 scale-75 group-hover/progress:scale-100"></div>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={displayedDuration || 100}
                    value={displayedTime}
                    onPointerDown={handleSeekPointerDown}
                    onPointerUp={handleSeekCommit}
                    onPointerCancel={handleSeekCancel}
                    onBlur={handleSeekCommit}
                    onChange={handleSeekPreview}
                    className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                  />
                </div>
                <span className="text-xs sm:text-sm font-medium text-white/90 min-w-[42px] sm:min-w-[50px] text-right font-mono">
                  {formatDuration(Math.floor(displayedDuration))}
                </span>
              </div>
            )}

            {/* Control buttons */}
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1 sm:gap-3 min-w-0">
                {isTimedMedia && (
                  <button
                    onClick={togglePlay}
                    className="p-2.5 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-all hover:scale-105"
                    title={isPlaying ? "Pause" : "Play"}
                  >
                    {isPlaying ? (
                      <Pause className="w-5 h-5 sm:w-6 sm:h-6" />
                    ) : (
                      <Play className="w-5 h-5 sm:w-6 sm:h-6" />
                    )}
                  </button>
                )}

                {isTimedMedia && (
                  <div className="flex items-center gap-2 group/vol">
                    <button
                      onClick={toggleMute}
                      className="p-2 rounded-lg hover:bg-white/10 text-white/80 hover:text-white transition-all"
                      title={isMuted ? "Unmute" : "Mute"}
                    >
                      {isMuted || volume === 0 ? (
                        <VolumeX className="w-5 h-5" />
                      ) : (
                        <Volume2 className="w-5 h-5" />
                      )}
                    </button>
                    <div className="w-0 overflow-hidden group-hover/vol:w-24 transition-all duration-300 hidden sm:block">
                      <input
                        type="range"
                        min={0}
                        max={1}
                        step={0.05}
                        value={isMuted ? 0 : volume}
                        onChange={handleVolumeChange}
                        className="w-20 h-1 bg-white/30 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:bg-white [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:shadow-lg"
                      />
                    </div>
                  </div>
                )}

                {/* Queue navigation is available only in the normal library. */}
                {!isTrashFile && (
                  <>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        playPreviousMedia();
                      }}
                      className="p-2 rounded-lg hover:bg-white/10 text-white/80 hover:text-white disabled:opacity-40 disabled:hover:bg-transparent transition-all"
                      title="Previous file"
                      disabled={!hasMultipleMedia}
                    >
                      <SkipBack className="w-5 h-5" />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        playNextMedia();
                      }}
                      className="p-2 rounded-lg hover:bg-white/10 text-white/80 hover:text-white disabled:opacity-40 disabled:hover:bg-transparent transition-all"
                      title="Next file"
                      disabled={!hasMultipleMedia}
                    >
                      <SkipForward className="w-5 h-5" />
                    </button>
                  </>
                )}

                {isImage && (
                  <span className="hidden sm:inline text-sm text-white/60 truncate">
                    Image
                  </span>
                )}
              </div>

              <div className="flex items-center gap-1 sm:gap-2 shrink-0">
                {/* Playback mode */}
                {isTimedMedia && !isTrashFile && (
                  <PlaybackModeButton
                    mode={currentPlaybackMode}
                    onClick={cyclePlaybackMode}
                  />
                )}

                {/* Speed */}
                {isTimedMedia && (
                  <button
                    onClick={cycleSpeed}
                    className={`flex items-center gap-1.5 px-2 sm:px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                      playbackSpeed !== 1
                        ? "bg-primary-500/30 text-primary-300 border border-primary-500/40"
                        : "bg-white/10 text-white/80 border border-white/10 hover:bg-white/20 hover:text-white"
                    }`}
                    title="Playback Speed"
                  >
                    <Gauge className="w-4 h-4" />
                    <span>{playbackSpeed}x</span>
                  </button>
                )}

                {/* PiP */}
                {isVideo && document.pictureInPictureEnabled && (
                  <button
                    onClick={togglePiP}
                    className={`p-2 rounded-lg transition-all ${
                      isPiP
                        ? "bg-primary-500/30 text-primary-300"
                        : "hover:bg-white/10 text-white/80 hover:text-white"
                    }`}
                    title="Picture in Picture"
                  >
                    <PictureInPicture2 className="w-5 h-5" />
                  </button>
                )}

                {/* Fullscreen */}
                <button
                  onClick={toggleFullscreen}
                  className="p-2 rounded-lg hover:bg-white/10 text-white/80 hover:text-white transition-all"
                  title={isFullscreen ? "Exit Fullscreen" : "Fullscreen"}
                >
                  {isFullscreen ? (
                    <Minimize className="w-5 h-5" />
                  ) : (
                    <Maximize className="w-5 h-5" />
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ImageViewer() {
  const {
    imageViewerFile: file,
    setImageViewerFile,
    imageQueue,
  } = useAppStore();
  const containerRef = useRef<HTMLDivElement>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [usingFallbackStream, setUsingFallbackStream] = useState(false);

  const isTrashImage = Boolean(file?.deleted_at);
  const imagePreviewQueueFromStore = imageQueue.filter(isImageFile);
  const imagePreviewQueue = isTrashImage
    ? file
      ? [file]
      : []
    : file && imagePreviewQueueFromStore.some((item) => item.id === file.id)
      ? imagePreviewQueueFromStore
      : file
        ? [file, ...imagePreviewQueueFromStore]
        : imagePreviewQueueFromStore;
  const currentImageIndex = file
    ? imagePreviewQueue.findIndex((item) => item.id === file.id)
    : -1;
  const hasMultipleImages =
    imagePreviewQueue.length > 1 && currentImageIndex !== -1;

  useEffect(() => {
    if (file) {
      setIsLoading(true);
      setError(null);
      setUsingFallbackStream(false);
    }
  }, [file]);

  if (!file) return null;

  const authorizedStreamUrl = toApiUrl(
    usingFallbackStream && file.fallback_stream_url
      ? file.fallback_stream_url
      : file.stream_url,
  );
  const downloadUrl = `${authorizedStreamUrl}${authorizedStreamUrl.includes("?") ? "&" : "?"}download=1`;

  const openImageAtIndex = (index: number) => {
    if (!hasMultipleImages) return;
    const safeIndex =
      ((index % imagePreviewQueue.length) + imagePreviewQueue.length) %
      imagePreviewQueue.length;
    const nextFile = imagePreviewQueue[safeIndex];
    if (nextFile) {
      setImageViewerFile(nextFile);
    }
  };

  const openPreviousImage = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    openImageAtIndex(currentImageIndex - 1);
  };

  const openNextImage = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    openImageAtIndex(currentImageIndex + 1);
  };

  const toggleFullscreen = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    if (!document.fullscreenElement) {
      containerRef.current?.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  };

  const closeViewer = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    setImageViewerFile(null);
  };

  return (
    <div
      ref={containerRef}
      className="fixed inset-0 bg-black z-[120] flex items-center justify-center font-sans animate-fade-in"
      onClick={(e) => {
        const target = e.target as HTMLElement | null;
        if (target?.closest('[data-image-viewer-control="true"]')) return;
      }}
    >
      {/* Header */}
      <div
        data-image-viewer-control="true"
        className="absolute top-0 left-0 right-0 p-3 sm:p-5 bg-gradient-to-b from-black/85 via-black/45 to-transparent z-20"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="text-white font-medium text-sm sm:text-base truncate">
              {file.file_name}
            </h3>
            <p className="text-xs text-white/50">Image preview</p>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <a
              href={downloadUrl}
              download={file.file_name}
              className="p-2 rounded-full text-white/80 hover:text-white hover:bg-white/10 transition-colors"
              title="Download"
              onClick={(e) => e.stopPropagation()}
            >
              <Download className="w-5 h-5" />
            </a>
            <a
              href={authorizedStreamUrl}
              target="_blank"
              rel="noreferrer"
              className="p-2 rounded-full text-white/80 hover:text-white hover:bg-white/10 transition-colors"
              title="Open image"
              onClick={(e) => e.stopPropagation()}
            >
              <ExternalLink className="w-5 h-5" />
            </a>
            <button
              onClick={toggleFullscreen}
              className="p-2 rounded-full text-white/80 hover:text-white hover:bg-white/10 transition-colors"
              title={isFullscreen ? "Exit Fullscreen" : "Fullscreen"}
            >
              {isFullscreen ? (
                <Minimize className="w-5 h-5" />
              ) : (
                <Maximize className="w-5 h-5" />
              )}
            </button>
            <button
              onClick={closeViewer}
              className="p-2 rounded-full text-white/80 hover:text-white hover:bg-white/10 transition-colors"
              title="Close"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>
      </div>

      {error ? (
        <div className="text-center p-8 max-w-md glass-panel z-10 animate-scale-in">
          <div className="w-16 h-16 rounded-2xl bg-yellow-500/20 flex items-center justify-center mx-auto mb-5 border border-yellow-500/30">
            <AlertTriangle className="w-8 h-8 text-yellow-400" />
          </div>
          <h3 className="text-xl font-bold text-white mb-2">
            Preview Not Supported
          </h3>
          <p className="text-dark-300 mb-6">{error}</p>
          <div className="flex gap-3">
            <a
              href={authorizedStreamUrl}
              target="_blank"
              rel="noreferrer"
              className="flex-1 btn-secondary flex items-center justify-center gap-2"
            >
              <ExternalLink className="w-4 h-4" />
              Open
            </a>
            <a
              href={downloadUrl}
              download={file.file_name}
              className="flex-1 btn-secondary flex items-center justify-center gap-2"
            >
              <Download className="w-4 h-4" />
              Download
            </a>
          </div>
        </div>
      ) : (
        <img
          key={`${file.id}-${usingFallbackStream ? "fallback" : "edge"}`}
          src={authorizedStreamUrl}
          alt={file.file_name}
          crossOrigin={mediaCrossOrigin}
          className="max-w-full max-h-full w-full h-full object-contain select-none p-2 sm:p-8"
          onLoad={() => setIsLoading(false)}
          onError={() => {
            if (!usingFallbackStream && file.fallback_stream_url) {
              setUsingFallbackStream(true);
              setIsLoading(true);
              setError(null);
              return;
            }
            setIsLoading(false);
            setError(
              "Browser cannot preview this image format. You can download or open the file directly.",
            );
          }}
          draggable={false}
        />
      )}

      {isLoading && !error && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none z-10">
          <div className="animate-spin rounded-full h-14 w-14 border-b-2 border-primary-500"></div>
        </div>
      )}

      {/* Bottom image controls */}
      <div
        data-image-viewer-control="true"
        className="absolute bottom-0 left-0 right-0 p-4 sm:p-6 bg-gradient-to-t from-black/85 via-black/45 to-transparent z-20"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-center gap-3">
          {!isTrashImage && (
            <button
              onClick={openPreviousImage}
              className="p-3 rounded-full bg-white/10 hover:bg-white/20 text-white disabled:opacity-40 disabled:hover:bg-white/10 transition-all"
              title="Previous image"
              disabled={!hasMultipleImages}
            >
              <SkipBack className="w-5 h-5" />
            </button>
          )}
          <button
            onClick={closeViewer}
            className="px-5 py-2.5 rounded-full bg-white/10 hover:bg-white/20 text-white text-sm font-medium transition-all"
            title="Close image"
          >
            Close
          </button>
          {!isTrashImage && (
            <button
              onClick={openNextImage}
              className="p-3 rounded-full bg-white/10 hover:bg-white/20 text-white disabled:opacity-40 disabled:hover:bg-white/10 transition-all"
              title="Next image"
              disabled={!hasMultipleImages}
            >
              <SkipForward className="w-5 h-5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function AudioArtworkPreview({
  thumbnailUrl,
  fileName,
  isPlaying,
}: {
  thumbnailUrl: string | null;
  fileName: string;
  isPlaying: boolean;
}) {
  return (
    <div className="w-64 h-64 mx-auto mb-6 rounded-2xl shadow-2xl overflow-hidden border border-white/10 relative group bg-dark-950">
      {thumbnailUrl ? (
        <img
          src={thumbnailUrl}
          alt={fileName}
          crossOrigin={mediaCrossOrigin}
          className="w-full h-full object-cover"
          draggable={false}
        />
      ) : (
        <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-dark-900 to-dark-950">
          <span className="text-6xl">🎵</span>
        </div>
      )}
      <div className="absolute inset-0 bg-gradient-to-t from-black/45 via-transparent to-black/10 pointer-events-none" />
      <div className="absolute bottom-4 left-4 right-4 flex items-center justify-center pointer-events-none">
        <span className="px-3 py-1 rounded-full bg-black/60 border border-white/10 text-white/80 text-xs font-medium backdrop-blur">
          {isPlaying ? "Playing" : "Paused"}
        </span>
      </div>
    </div>
  );
}

function MiniAudioArtwork({
  thumbnailUrl,
  fileName,
}: {
  thumbnailUrl: string | null;
  fileName: string;
  isPlaying: boolean;
}) {
  if (thumbnailUrl) {
    return (
      <img
        src={thumbnailUrl}
        alt={fileName}
        crossOrigin={mediaCrossOrigin}
        className="w-full h-full object-cover"
        draggable={false}
      />
    );
  }

  return <span className="text-2xl">🎵</span>;
}

function PlaybackModeButton({
  mode,
  onClick,
  compact = false,
}: {
  mode: PlaybackModeOption;
  onClick: (e: React.MouseEvent<HTMLButtonElement>) => void;
  compact?: boolean;
}) {
  if (compact) {
    return (
      <button
        onClick={onClick}
        className="p-2 text-dark-300 hover:text-white rounded-lg hover:bg-white/10 transition-colors"
        title={`${mode.label}: ${mode.description}`}
      >
        <span className="text-base leading-none">{mode.icon}</span>
      </button>
    );
  }

  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 px-2 sm:px-3 py-1.5 rounded-lg text-sm font-medium border transition-all ${
        mode.value !== "normal"
          ? "bg-primary-500/30 text-primary-300 border-primary-500/40"
          : "bg-white/10 text-white/80 border-white/10 hover:bg-white/20 hover:text-white"
      }`}
      title={`${mode.label}: ${mode.description}`}
    >
      <span className="text-base leading-none">{mode.icon}</span>
      <span className="hidden sm:inline whitespace-nowrap">
        {mode.shortLabel}
      </span>
    </button>
  );
}

function TenSecondButton({
  direction,
  onClick,
}: {
  direction: "back" | "forward";
  onClick: (e: React.MouseEvent<HTMLButtonElement>) => void;
}) {
  const arrow = direction === "back" ? "↺" : "↻";
  const title = direction === "back" ? "Back 10 seconds" : "Forward 10 seconds";

  return (
    <button
      onClick={onClick}
      className="w-12 h-12 sm:w-14 sm:h-14 rounded-full bg-black/35 border border-white/20 backdrop-blur flex flex-col items-center justify-center text-white/80 hover:text-white hover:bg-white/15 transition-all"
      title={title}
    >
      <span className="text-base leading-3">{arrow}</span>
      <span className="text-sm font-bold leading-4">10</span>
    </button>
  );
}

// Helper button component for cleaner code
function Button({
  onClick,
  className,
  children,
}: {
  onClick?: (e: any) => void;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <button onClick={onClick} className={className}>
      {children}
    </button>
  );
}
