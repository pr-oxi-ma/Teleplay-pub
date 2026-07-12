import {
  Archive,
  BarChart3,
  CalendarDays,
  FileText,
  Film,
  FolderOpen,
  HardDrive,
  Image as ImageIcon,
  MousePointerClick,
  Music,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useState } from "react";
import {
  formatFileSize,
  mediaCrossOrigin,
  toApiUrl,
  useStorageAnalytics,
} from "../lib/api";

const TYPE_STYLES: Record<string, { color: string; bar: string; icon: typeof Film }> = {
  video: { color: "text-primary-300", bar: "bg-primary-500", icon: Film },
  audio: { color: "text-emerald-300", bar: "bg-emerald-500", icon: Music },
  image: { color: "text-pink-300", bar: "bg-pink-500", icon: ImageIcon },
  document: { color: "text-amber-300", bar: "bg-amber-500", icon: FileText },
};

const fallbackTypeStyle = {
  color: "text-dark-300",
  bar: "bg-dark-500",
  icon: Archive,
};

const parseActivityDate = (value: string) => new Date(`${value}T00:00:00`);

const shortDate = (value: string) =>
  new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" }).format(
    parseActivityDate(value),
  );

const longDate = (value: string) =>
  new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(parseActivityDate(value));

interface LargestFilePreviewProps {
  fileName: string;
  thumbnailUrl: string | null;
  icon: typeof Film;
  iconClassName: string;
}

function LargestFilePreview({
  fileName,
  thumbnailUrl,
  icon: Icon,
  iconClassName,
}: LargestFilePreviewProps) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const resolvedThumbnailUrl =
    thumbnailUrl && !thumbnailFailed ? toApiUrl(thumbnailUrl) : null;

  return (
    <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded-xl border border-white/[0.06] bg-dark-800">
      {resolvedThumbnailUrl ? (
        <img
          src={resolvedThumbnailUrl}
          alt=""
          aria-hidden="true"
          crossOrigin={mediaCrossOrigin}
          className="h-full w-full object-cover"
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => setThumbnailFailed(true)}
        />
      ) : (
        <div
          className="flex h-full w-full items-center justify-center"
          role="img"
          aria-label={`${fileName} file type`}
        >
          <Icon className={`h-5 w-5 ${iconClassName}`} />
        </div>
      )}
    </div>
  );
}

export default function StorageAnalytics() {
  const { data, isLoading, isFetching, refetch } = useStorageAnalytics();
  const [selectedActivityDate, setSelectedActivityDate] = useState<string | null>(null);

  if (isLoading || !data) {
    return (
      <div className="mx-auto grid w-full max-w-6xl gap-4 md:grid-cols-2 xl:grid-cols-4">
        {[...Array(8)].map((_, index) => (
          <div key={index} className="h-32 animate-pulse rounded-2xl border border-white/[0.05] bg-dark-800/50" />
        ))}
      </div>
    );
  }

  const telegramTotal = data.active.size + data.trash.size;
  const largestTypeSize = Math.max(1, ...data.by_type.map((item) => item.size));
  const largestDailyCount = Math.max(1, ...data.daily_activity.map((item) => item.count));
  const selectedActivity = selectedActivityDate
    ? data.daily_activity.find((item) => item.date === selectedActivityDate) ?? null
    : null;
  const selectedAverageSize = selectedActivity?.count
    ? selectedActivity.size / selectedActivity.count
    : 0;

  const summaryCards = [
    {
      label: "Telegram storage",
      value: formatFileSize(telegramTotal),
      detail: "Active + Recycle Bin",
      icon: HardDrive,
      tone: "text-primary-300 bg-primary-500/10 border-primary-500/15",
    },
    {
      label: "Active files",
      value: data.active.count.toLocaleString(),
      detail: formatFileSize(data.active.size),
      icon: Archive,
      tone: "text-emerald-300 bg-emerald-500/10 border-emerald-500/15",
    },
    {
      label: "Recycle Bin",
      value: data.trash.count.toLocaleString(),
      detail: formatFileSize(data.trash.size),
      icon: Trash2,
      tone: "text-red-300 bg-red-500/10 border-red-500/15",
    },
    {
      label: "Folders",
      value: data.folders.toLocaleString(),
      detail: "Active hierarchy",
      icon: FolderOpen,
      tone: "text-amber-300 bg-amber-500/10 border-amber-500/15",
    },
  ];

  return (
    <div className="mx-auto w-full min-w-0 max-w-6xl animate-fade-in pb-20">
      <div className="mb-5 flex min-w-0 flex-col gap-4 overflow-hidden rounded-2xl border border-white/[0.06] bg-dark-900/60 p-4 shadow-xl shadow-black/10 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="rounded-xl border border-primary-500/20 bg-primary-500/10 p-2.5 text-primary-300">
            <BarChart3 className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-lg font-bold text-white">Storage Analytics</h1>
            <p className="mt-1 text-sm text-dark-400">
              Understand what is using your Telegram-backed library.
            </p>
          </div>
        </div>
        <button
          onClick={() => refetch()}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-dark-200 transition-colors hover:bg-white/[0.06] hover:text-white"
        >
          <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {summaryCards.map(({ label, value, detail, icon: Icon, tone }) => (
          <article key={label} className="min-w-0 overflow-hidden rounded-2xl border border-white/[0.06] bg-dark-900/55 p-4 shadow-lg shadow-black/10">
            <div className={`mb-4 inline-flex rounded-xl border p-2.5 ${tone}`}><Icon className="h-5 w-5" /></div>
            <p className="text-xs font-medium uppercase tracking-[0.14em] text-dark-500">{label}</p>
            <p className="mt-1 text-2xl font-bold tracking-tight text-white">{value}</p>
            <p className="mt-1 text-xs text-dark-400">{detail}</p>
          </article>
        ))}
      </div>

      <div className="mt-4 grid min-w-0 gap-4 lg:grid-cols-5">
        <section className="min-w-0 overflow-hidden rounded-2xl border border-white/[0.06] bg-dark-900/55 p-4 lg:col-span-3">
          <div className="mb-5">
            <h2 className="font-semibold text-white">Storage by type</h2>
            <p className="mt-1 text-xs text-dark-400">Active files only</p>
          </div>
          {data.by_type.length ? (
            <div className="space-y-4">
              {data.by_type.map((item) => {
                const style = TYPE_STYLES[item.type] || fallbackTypeStyle;
                const Icon = style.icon;
                const width = Math.max(3, (item.size / largestTypeSize) * 100);
                return (
                  <div key={item.type}>
                    <div className="mb-2 flex min-w-0 items-start gap-2 text-sm">
                      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${style.color}`} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate capitalize text-dark-200">{item.type}</p>
                        <p className="mt-0.5 text-xs text-dark-500">{item.count} files</p>
                      </div>
                      <span className="shrink-0 text-right font-medium text-white">
                        {formatFileSize(item.size)}
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-dark-800">
                      <div className={`h-full rounded-full ${style.bar}`} style={{ width: `${width}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex h-40 items-center justify-center text-sm text-dark-500">No active files yet</div>
          )}
        </section>

        <section className="min-w-0 overflow-hidden rounded-2xl border border-white/[0.06] bg-dark-900/55 p-4 lg:col-span-2">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="font-semibold text-white">Upload activity</h2>
              <p className="mt-1 text-xs text-dark-400">Last 14 days · bar height shows uploads</p>
            </div>
            {selectedActivity && (
              <button
                type="button"
                onClick={() => setSelectedActivityDate(null)}
                className="shrink-0 rounded-md px-2 py-1 text-[11px] font-medium text-dark-400 transition-colors hover:bg-white/[0.05] hover:text-white"
              >
                Clear
              </button>
            )}
          </div>

          <div
            className={`mt-4 min-h-[76px] rounded-xl border p-3 transition-colors ${
              selectedActivity
                ? "border-primary-500/25 bg-primary-500/[0.08]"
                : "border-white/[0.06] bg-dark-800/45"
            }`}
            aria-live="polite"
          >
            {selectedActivity ? (
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-2">
                  <div className="rounded-lg bg-primary-500/15 p-1.5 text-primary-300">
                    <CalendarDays className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-white">{longDate(selectedActivity.date)}</p>
                    <p className="text-[11px] text-dark-400">Selected activity</p>
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-3 gap-2">
                  <div className="min-w-0 rounded-lg bg-black/10 px-2 py-2">
                    <p className="text-[10px] uppercase tracking-wide text-dark-500">Uploads</p>
                    <p className="mt-0.5 truncate text-sm font-semibold text-white">
                      {selectedActivity.count.toLocaleString()}
                    </p>
                  </div>
                  <div className="min-w-0 rounded-lg bg-black/10 px-2 py-2">
                    <p className="text-[10px] uppercase tracking-wide text-dark-500">Uploaded</p>
                    <p className="mt-0.5 truncate text-sm font-semibold text-white">
                      {formatFileSize(selectedActivity.size)}
                    </p>
                  </div>
                  <div className="min-w-0 rounded-lg bg-black/10 px-2 py-2">
                    <p className="text-[10px] uppercase tracking-wide text-dark-500">Average</p>
                    <p className="mt-0.5 truncate text-sm font-semibold text-white">
                      {selectedActivity.count ? formatFileSize(selectedAverageSize) : "—"}
                    </p>
                  </div>
                </div>
                {!selectedActivity.count && (
                  <p className="mt-2 text-xs text-dark-400">No files were uploaded on this day.</p>
                )}
              </div>
            ) : (
              <div className="flex min-h-[50px] items-center gap-3">
                <div className="rounded-lg bg-dark-700/70 p-2 text-dark-300">
                  <MousePointerClick className="h-4 w-4" />
                </div>
                <div>
                  <p className="text-sm font-medium text-dark-200">Tap a bar to inspect that day</p>
                  <p className="mt-0.5 text-xs leading-relaxed text-dark-500">
                    See the date, upload count, total uploaded size and average file size.
                  </p>
                </div>
              </div>
            )}
          </div>

          <div className="mt-4 flex h-44 min-w-0 items-end gap-1.5 overflow-hidden">
            {data.daily_activity.map((item, index) => {
              const height = item.count ? Math.max(8, (item.count / largestDailyCount) * 100) : 3;
              const isSelected = selectedActivityDate === item.date;
              const showDate = isSelected || index === 0 || index === 6 || index === 13;
              const activityLabel = `${longDate(item.date)}: ${item.count} ${
                item.count === 1 ? "upload" : "uploads"
              }, ${formatFileSize(item.size)}`;

              return (
                <button
                  key={item.date}
                  type="button"
                  onClick={() => setSelectedActivityDate(item.date)}
                  className="group flex min-w-0 flex-1 touch-manipulation flex-col items-center justify-end gap-2 rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400/80"
                  title={activityLabel}
                  aria-label={activityLabel}
                  aria-pressed={isSelected}
                >
                  <span
                    className={`relative flex h-32 w-full items-end justify-center rounded-md px-0.5 transition-colors ${
                      isSelected
                        ? "bg-primary-500/15 ring-1 ring-inset ring-primary-400/40"
                        : "bg-dark-800/50 group-hover:bg-dark-800/80"
                    }`}
                  >
                    <span
                      className={`w-full rounded-md transition-all duration-200 ${
                        isSelected
                          ? "bg-primary-400 shadow-[0_0_16px_rgba(168,85,247,0.24)]"
                          : item.count
                            ? "bg-primary-500 group-hover:bg-primary-400"
                            : "bg-dark-700 group-hover:bg-dark-600"
                      }`}
                      style={{ height: `${height}%` }}
                    />
                  </span>
                  <span
                    className={`w-full truncate text-center text-[9px] transition-colors ${
                      showDate
                        ? isSelected
                          ? "font-semibold text-primary-300"
                          : "text-dark-500"
                        : "invisible"
                    }`}
                  >
                    {shortDate(item.date)}
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      </div>

      <section className="mt-4 min-w-0 overflow-hidden rounded-2xl border border-white/[0.06] bg-dark-900/55">
        <div className="border-b border-white/[0.05] p-4">
          <h2 className="font-semibold text-white">Largest files</h2>
          <p className="mt-1 text-xs text-dark-400">Top active files by size</p>
        </div>
        {data.largest_files.length ? (
          <div className="divide-y divide-white/[0.05]">
            {data.largest_files.map((file, index) => {
              const style = TYPE_STYLES[file.file_type] || fallbackTypeStyle;
              const Icon = style.icon;
              return (
                <div key={file.id} className="flex min-w-0 items-center gap-3 px-4 py-3 transition-colors hover:bg-white/[0.02]">
                  <span className="w-5 text-xs font-semibold text-dark-600">{index + 1}</span>
                  <LargestFilePreview
                    fileName={file.file_name}
                    thumbnailUrl={file.thumbnail_url}
                    icon={Icon}
                    iconClassName={style.color}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-white" title={file.file_name}>{file.file_name}</p>
                    <p className="mt-0.5 text-xs capitalize text-dark-500">{file.file_type}</p>
                  </div>
                  <span className="shrink-0 text-sm font-semibold text-dark-200">{formatFileSize(file.file_size)}</span>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="p-8 text-center text-sm text-dark-500">No active files yet</div>
        )}
      </section>
    </div>
  );
}
